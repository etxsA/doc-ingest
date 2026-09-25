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


class BrokenEP:
    """An installed plugin whose module imports a missing optional dependency."""

    name = "fancy-ocr"
    value = "brokenplug_mod:factory"
    loads = 0

    @classmethod
    def load(cls):
        cls.loads += 1
        raise ModuleNotFoundError("No module named 'some_optional_gpu_lib'")


@pytest.fixture
def broken_plugin(monkeypatch):
    BrokenEP.loads = 0
    monkeypatch.setattr(
        "docingest.bootstrap.entry_points",
        lambda group: [BrokenEP] if group == "docingest.ocr" else [],
    )
    return BrokenEP


def test_a_broken_plugin_does_not_break_the_selected_builtin(broken_plugin, cfg):
    cfg.adapters.ocr = "openai-compatible"
    assert type(build("ocr", cfg)).__name__ == "OpenAICompatibleOcr"
    assert Container(cfg, log=lambda _: None).ingest.ocr is not None
    assert "fancy-ocr" in available("ocr")  # listed by name
    assert broken_plugin.loads == 0  # never imported


def test_selecting_a_broken_plugin_raises_its_import_error_with_context(broken_plugin, cfg):
    cfg.adapters.ocr = "fancy-ocr"
    with pytest.raises(ModuleNotFoundError) as info:
        build("ocr", cfg)
    assert any("fancy-ocr" in note for note in info.value.__notes__)


def test_a_plugin_named_like_a_builtin_is_not_imported(monkeypatch, cfg):
    class Shadow(BrokenEP):
        name = "openai-compatible"
        loads = 0

    monkeypatch.setattr("docingest.bootstrap.entry_points", lambda group: [Shadow])
    cfg.adapters.ocr = "openai-compatible"
    assert type(build("ocr", cfg)).__name__ == "OpenAICompatibleOcr"
    assert Shadow.loads == 0
