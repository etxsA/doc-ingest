"""The entry points and what docingest builds from the three config tables."""

import pytest
from docingest.bootstrap import Container, available, build
from docingest.config import AppConfig

from docingest_index.embedder import OpenAICompatibleEmbedder
from docingest_index.local import LocalIndex
from docingest_index.reranker import VllmReranker

SELECTED = {"embedder": "openai-compatible", "index": "local", "reranker": "vllm"}


def config(tmp_path, **tables):
    tables.setdefault("index", {"dir": str(tmp_path / "idx")})
    return AppConfig.model_validate({"adapters": SELECTED, **tables})


def test_docingest_lists_the_three_adapters_next_to_its_none_adapters():
    assert "openai-compatible" in available("embedder") and "none" in available("embedder")
    assert "local" in available("index") and "none" in available("index")
    assert "vllm" in available("reranker") and "none" in available("reranker")


def test_the_selected_adapters_are_built_from_the_config_tables(tmp_path):
    cfg = config(
        tmp_path,
        embedder={"base_url": "http://127.0.0.1:9002/v1", "served_model": "embed-x"},
        reranker={"base_url": "http://127.0.0.1:9003", "instruction": "Find it"},
    )
    embedder, index, reranker = (build(port, cfg) for port in ("embedder", "index", "reranker"))
    assert isinstance(embedder, OpenAICompatibleEmbedder)
    assert embedder.url == "http://127.0.0.1:9002/v1/embeddings"
    assert isinstance(reranker, VllmReranker) and reranker.url == "http://127.0.0.1:9003/rerank"
    assert isinstance(index, LocalIndex)
    assert index.embedder_fingerprint == embedder.fingerprint  # the pair IndexService checks
    assert str(index.folder).startswith(str(tmp_path / "idx"))


def test_the_index_holds_the_chunks_ask_cuts_so_it_follows_the_qa_settings(tmp_path):
    base = build("index", config(tmp_path))
    longer = build("index", config(tmp_path, qa={"chunk_chars": 1200}))
    assert base.folder != longer.folder
    assert build("index", config(tmp_path)).folder == base.folder


def test_nothing_is_required_in_the_config(tmp_path):
    cfg = AppConfig.model_validate({"adapters": SELECTED})
    index = build("index", cfg)
    assert str(index.folder).startswith("data/index/qwen-qwen3-embedding-4b-")
    assert build("embedder", cfg).fingerprint == index.embedder_fingerprint


@pytest.mark.parametrize(
    ("tables", "message"),
    [
        ({"index": {"bm25": "sometimes"}}, r"^\[index\] bm25: "),
        ({"embedder": {"model": "org/other"}}, r"^\[embedder\] table: .*revision is required"),
        ({"reranker": {"nope": 1}}, r"^\[reranker\] nope: Extra inputs"),
    ],
)
def test_a_bad_table_is_an_error_that_names_it(tmp_path, tables, message):
    cfg = config(tmp_path, **tables)
    port = next(iter(tables)) if "index" not in tables else "index"
    with pytest.raises(ValueError, match=message):
        build(port, cfg)


def test_the_container_wires_the_index_service_with_the_real_adapters(tmp_path):
    container = Container(config(tmp_path), log=lambda _: None)
    service = container.index_service
    assert isinstance(service.embedder, OpenAICompatibleEmbedder)
    assert isinstance(service.index, LocalIndex)
    assert (service.chunk_chars, service.overlap) == (900, 100)


def test_two_corpora_sharing_the_index_dir_get_separate_indexes(tmp_path):
    def cfg(corpus):
        return AppConfig.model_validate(
            {
                "output_dir": str(tmp_path / corpus),
                "adapters": SELECTED,
                "index": {"dir": str(tmp_path / "idx")},
            }
        )

    one, two = build("index", cfg("stage1")), build("index", cfg("qasper"))
    assert one.folder != two.folder and one.folder.parent == two.folder.parent
    assert build("index", cfg("stage1")).folder == one.folder
    assert one.corpus == str((tmp_path / "stage1").resolve())
