import pytest
from fakes import FakeOcr

from docingest.bootstrap import REGISTRY, Container, available, build
from docingest.config import AppConfig, OcrConfig


def test_every_port_has_its_selected_default_registered():
    cfg = AppConfig()
    for port in REGISTRY:
        assert getattr(cfg.adapters, port) in available(port)


def test_unknown_adapter_name_is_a_clear_error(cfg):
    cfg.adapters.ocr = "does-not-exist"
    with pytest.raises(ValueError, match="available"):
        build("ocr", cfg)


def test_overrides_replace_any_port(cfg):
    fake = FakeOcr()
    container = Container(cfg, log=lambda _: None, overrides={"ocr": fake})
    assert container.ingest.ocr is fake


def test_entry_point_plugins_are_discovered(monkeypatch):
    class EP:
        name = "my-ocr"

        @staticmethod
        def load():
            return lambda cfg: FakeOcr(model="plugin/ocr")

    monkeypatch.setattr(
        "docingest.bootstrap.entry_points",
        lambda group: [EP] if group == "docingest.ocr" else [],
    )
    assert "my-ocr" in available("ocr")
    cfg = AppConfig()
    cfg.adapters.ocr = "my-ocr"
    assert build("ocr", cfg).model.repo_id == "plugin/ocr"


def test_ocr_revision_required_when_swapping_models():
    with pytest.raises(ValueError, match="revision"):
        OcrConfig(repo_id="mlx-community/other-model")
    assert OcrConfig().revision  # default model is pinned
