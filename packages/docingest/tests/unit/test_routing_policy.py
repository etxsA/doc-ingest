import pytest

from docingest.domain.models import PageSignals
from docingest.domain.routing import RoutingPolicy, decide

POLICY = RoutingPolicy()


@pytest.mark.parametrize(
    ("signals", "needs_ocr", "reason"),
    [
        (PageSignals(n_chars=3000, alpha_ratio=0.8), False, None),
        (PageSignals(n_chars=0, image_coverage=1.0, n_images=1), True, "embedded chars"),
        (PageSignals(n_chars=120, image_coverage=0.9, alpha_ratio=0.8), True, "image covers"),
        (PageSignals(n_chars=2000, image_coverage=0.9, alpha_ratio=0.8), False, None),
        (PageSignals(n_chars=900, garbage_ratio=0.4, alpha_ratio=0.8), True, "garbled"),
        (PageSignals(n_chars=900, alpha_ratio=0.2), True, "alphabetic"),
    ],
)
def test_decide(signals, needs_ocr, reason):
    probe = decide(signals, POLICY)
    assert probe.needs_ocr is needs_ocr
    if reason:
        assert any(reason in r for r in probe.reasons)


def test_force_ocr_records_reason():
    probe = decide(PageSignals(n_chars=3000, alpha_ratio=0.8), POLICY, force_ocr=True)
    assert probe.needs_ocr and probe.reasons == ["forced (--ocr-all)"]


def test_policy_thresholds_are_configurable():
    lenient = RoutingPolicy(min_chars=0, min_alpha_ratio=0)
    assert not decide(PageSignals(n_chars=0), lenient).needs_ocr


def test_an_unknown_routing_key_is_rejected():
    # Old: a misspelt [routing] key was dropped silently and the default threshold applied.
    from pydantic import ValidationError

    from docingest.config import AppConfig

    with pytest.raises(ValidationError, match="min_char"):
        AppConfig.model_validate({"routing": {"min_char": 0}})
