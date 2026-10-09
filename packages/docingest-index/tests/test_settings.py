import pytest
from docingest.config import AppConfig

from docingest_index import settings as s


def test_the_defaults_are_the_measured_configuration():
    index = s.IndexSettings()
    assert (index.dir, index.candidates, index.contexts, index.bm25) == (
        "data/index",
        50,
        10,
        "english",
    )
    assert index.max_chunks_per_paper == s.DEFAULT_MAX_CHUNKS_PER_PAPER == 0
    embedder = s.EmbedderSettings()
    assert (embedder.model, embedder.revision) == (
        "Qwen/Qwen3-Embedding-4B",
        "5cf2132abc99cad020ac570b19d031efec650f2b",
    )
    assert "dims" not in s.EmbedderSettings.model_fields  # the index learns the length
    assert (embedder.served_model, embedder.query_instruction) == ("qwen-embed", "")
    assert (embedder.batch_size, embedder.concurrency) == (64, 8)
    reranker = s.RerankerSettings()
    assert (reranker.model, reranker.revision) == (
        "Qwen/Qwen3-Reranker-8B",
        "77d193c791ed757ca307ee72715aa132723da912",
    )
    assert reranker.served_model == "qwen-rerank" and "research paper" in reranker.instruction
    assert reranker.base_url == "http://127.0.0.1:8003"


def test_another_model_needs_its_own_pinned_revision():
    with pytest.raises(ValueError, match="revision is required for 'org/other'"):
        s.EmbedderSettings(model="org/other")
    with pytest.raises(ValueError, match="revision is required"):
        s.RerankerSettings(model="org/other")
    assert s.EmbedderSettings(model="org/other", revision="c" * 40).revision == "c" * 40


def test_unknown_keys_and_bad_values_are_refused():
    for bad in (
        {"bm25": "sometimes"},
        {"candidates": 0},
        {"typo": 1},
        {"max_chunks_per_paper": -1},
    ):
        with pytest.raises(ValueError):  # noqa: PT011  (pydantic's ValidationError is a ValueError)
            s.IndexSettings(**bad)
    with pytest.raises(ValueError):  # noqa: PT011
        s.EmbedderSettings(batch_size=0)


def test_load_reads_the_table_of_the_config_and_names_the_table_in_errors():
    cfg = AppConfig.model_validate({"index": {"candidates": 30}, "embedder": {"typo": 1}})
    assert s.load(cfg, "index", s.IndexSettings).candidates == 30
    assert s.load(cfg, "reranker", s.RerankerSettings).served_model == "qwen-rerank"  # missing
    with pytest.raises(ValueError, match=r"^\[embedder\] typo: Extra inputs are not permitted"):
        s.load(cfg, "embedder", s.EmbedderSettings)
    bad = AppConfig.model_validate({"embedder": {"model": "org/other"}})
    with pytest.raises(ValueError, match=r"^\[embedder\] table: .*revision is required"):
        s.load(bad, "embedder", s.EmbedderSettings)


def test_candidates_and_contexts_have_one_definition_in_docingest():
    from docingest.config import DEFAULT_CANDIDATES, DEFAULT_CONTEXTS, IndexConfig

    plugin = s.IndexSettings()
    assert (plugin.candidates, plugin.contexts) == (DEFAULT_CANDIDATES, DEFAULT_CONTEXTS)
    assert issubclass(s.IndexSettings, IndexConfig)
    cfg = AppConfig.model_validate({"index": {"candidates": 30, "contexts": 4, "bm25": "never"}})
    from docingest.config import index_config

    # what ask reads and what the adapter validates are the same two values
    assert index_config(cfg).candidates == s.load(cfg, "index", s.IndexSettings).candidates == 30
    assert index_config(cfg).contexts == s.load(cfg, "index", s.IndexSettings).contexts == 4
    with pytest.raises(ValueError, match=r"\[index\].*typo"):
        s.load(AppConfig.model_validate({"index": {"typo": 1}}), "index", s.IndexSettings)
