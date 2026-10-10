from pathlib import Path

import pytest
from fakes import FakeOcr

from docingest.adapters.retrieval.none import NoIndex
from docingest.bootstrap import REGISTRY, Container, available, build
from docingest.config import AppConfig, OcrConfig, load_config
from docingest.domain.chunking import Chunk
from docingest.domain.errors import InvalidConfigError, NotConfiguredError
from docingest.ports import ChunkIndex, Embedder, Reranker


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


def test_the_callers_overrides_are_not_filled_with_built_adapters(cfg):
    # Old: the dict was the adapter cache, so a second Container given the same dict got
    # the first one's adapters, built from the first one's config.
    overrides = {"ocr": FakeOcr()}
    first = Container(cfg, log=lambda _: None, overrides=overrides)
    assert first.adapter("store").root == Path(cfg.output_dir)
    assert list(overrides) == ["ocr"]
    other = cfg.model_copy(update={"output_dir": str(Path(cfg.output_dir) / "other")})
    second = Container(other, log=lambda _: None, overrides=overrides)
    assert second.adapter("store").root == Path(other.output_dir)
    assert second.adapter("ocr") is first.adapter("ocr") is overrides["ocr"]


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


RETRIEVAL_PORTS = {"embedder": Embedder, "index": ChunkIndex, "reranker": Reranker}


def test_no_chunk_index_is_configured_by_default():
    cfg = AppConfig()
    assert {getattr(cfg.adapters, port) for port in RETRIEVAL_PORTS} == {"none"}


@pytest.mark.parametrize("port", RETRIEVAL_PORTS)
def test_the_none_adapter_of_each_retrieval_port_is_built_and_satisfies_the_port(port, cfg):
    adapter = Container(cfg, log=lambda _: None).adapter(port)
    assert isinstance(adapter, RETRIEVAL_PORTS[port])


def test_the_none_adapters_refuse_to_work_and_name_the_setting(cfg):
    container = Container(cfg, log=lambda _: None)
    chunk = Chunk("d" * 64, "d pages 1-1", "text", 1, 1)
    calls = {
        "embedder": [
            lambda a: a.embed_documents(["x"]),
            lambda a: a.embed_query("x"),
        ],
        "index": [
            lambda a: a.keys(),
            lambda a: a.begin_write(),
            lambda a: a.upsert("d", "k", [chunk], [[0.0]]),
            lambda a: a.remove("d"),
            lambda a: a.commit(),
            lambda a: a.stats(),
            lambda a: a.committed(),
            lambda a: a.search("q", [0.0], 5),
            lambda a: a.search("q", [0.0], 5, keywords="never"),
        ],
        "reranker": [lambda a: a.rerank("q", [chunk])],
    }
    for port, operations in calls.items():
        adapter = container.adapter(port)
        for operation in operations:
            with pytest.raises(NotConfiguredError, match=rf'\[adapters\] {port} is "none"'):
                operation(adapter)


def test_the_retrieval_tables_reach_a_plugin_untouched(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.toml"
    path.write_text(
        '[adapters]\nindex = "local"\n'
        '[index]\ndir = "data/index"\ncandidates = 50\n'
        '[embedder]\nbase_url = "http://127.0.0.1:8002/v1"\n'
        '[reranker]\ninstruction = "Given a question"\n'
    )
    seen = {}

    class EP:
        name = "local"

        @staticmethod
        def load():
            def factory(cfg):
                seen.update(cfg.model_extra)
                return NoIndex()

            return factory

    cfg = load_config(path)
    assert cfg.model_extra == {
        "index": {"dir": "data/index", "candidates": 50},
        "embedder": {"base_url": "http://127.0.0.1:8002/v1"},
        "reranker": {"instruction": "Given a question"},
    }
    monkeypatch.setattr(
        "docingest.bootstrap.entry_points",
        lambda group: [EP] if group == "docingest.index" else [],
    )
    build("index", cfg)
    assert seen == cfg.model_extra


def test_closing_the_none_index_is_not_an_error(cfg):
    NoIndex().close()  # callers close in a finally, also when nothing is configured


def test_ask_has_no_index_unless_one_is_selected(cfg):
    assert Container(cfg, log=lambda _: None).ask.has_index is False
    cfg.adapters.embedder = "fake"  # an embedder alone does not change how ask answers
    assert Container(cfg, log=lambda _: None).ask.has_index is False


def test_selecting_an_index_makes_ask_retrieve_with_the_configured_stages(cfg):
    from fakes import FakeEmbedder, FakeIndex, FakeReranker

    cfg = AppConfig.model_validate(
        {
            "adapters": {"index": "fake"},
            "index": {"candidates": 7, "contexts": 3, "max_chunks_per_paper": 2, "bm25": "never"},
        }
    )
    parts = {"embedder": FakeEmbedder(), "index": FakeIndex(), "reranker": FakeReranker()}
    container = Container(cfg, log=lambda _: None, overrides=parts)
    assert container.ask.has_index is True
    retrieval = container.ask._retrieval()
    assert (retrieval.candidates, retrieval.contexts) == (7, 3)
    assert retrieval.max_chunks_per_paper == 2
    assert retrieval.embedder is parts["embedder"] and retrieval.index is parts["index"]
    assert retrieval.reranker is parts["reranker"]


def test_a_reranker_that_is_none_is_no_reranker_at_all():
    from fakes import FakeEmbedder, FakeIndex

    cfg = AppConfig.model_validate({"adapters": {"index": "fake"}})
    container = Container(
        cfg, log=lambda _: None, overrides={"embedder": FakeEmbedder(), "index": FakeIndex()}
    )
    retrieval = container.ask._retrieval()
    assert retrieval.reranker is None
    assert (retrieval.candidates, retrieval.contexts) == (50, 10)
    assert retrieval.max_chunks_per_paper == 0


def test_an_index_without_an_embedder_is_reported_when_the_question_needs_it(cfg):
    from fakes import FakeIndex

    cfg = AppConfig.model_validate({"adapters": {"index": "fake"}})
    container = Container(cfg, log=lambda _: None, overrides={"index": FakeIndex()})
    assert container.ask.has_index is True  # building the service raises nothing ...
    with pytest.raises(NotConfiguredError, match=r'index is "fake" but embedder is "none"'):
        container.ask._retrieval()  # ... using the index does


def test_a_bad_index_table_is_an_error_naming_it():
    from fakes import FakeEmbedder, FakeIndex

    cfg = AppConfig.model_validate({"adapters": {"index": "fake"}, "index": {"contexts": 0}})
    container = Container(
        cfg, log=lambda _: None, overrides={"embedder": FakeEmbedder(), "index": FakeIndex()}
    )
    with pytest.raises(InvalidConfigError, match=r"\[index\] contexts"):
        container.ask._retrieval()


def test_an_adapter_that_cannot_be_built_from_its_table_is_a_config_error():
    from fakes import FakeIndex

    cfg = AppConfig.model_validate({"adapters": {"index": "fake", "embedder": "no-such-adapter"}})
    container = Container(cfg, log=lambda _: None, overrides={"index": FakeIndex()})
    with pytest.raises(InvalidConfigError, match="unknown embedder adapter"):
        container.ask._retrieval()
