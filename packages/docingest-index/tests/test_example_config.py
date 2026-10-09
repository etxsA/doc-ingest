"""The example configurations load, name real adapters and leak nothing about a machine."""

import re
from pathlib import Path

import pytest
from docingest.bootstrap import available
from docingest.config import index_config, load_config

from docingest_index import settings as s

CONFIG = Path(__file__).resolve().parents[2] / "docingest" / "config"
EXAMPLES = sorted((CONFIG / "examples").glob("*.toml"))


def test_the_lab_server_example_configures_the_whole_index_path():
    cfg = load_config(CONFIG / "examples" / "lab-server.toml")
    assert (cfg.adapters.embedder, cfg.adapters.index, cfg.adapters.reranker) == (
        "openai-compatible",
        "local",
        "vllm",
    )
    for port in ("embedder", "index", "reranker"):
        assert getattr(cfg.adapters, port) in available(port)
    index = s.load(cfg, "index", s.IndexSettings)
    assert (index.candidates, index.contexts, index.bm25) == (50, 10, "english")
    assert index_config(cfg).contexts == 10
    embedder = s.load(cfg, "embedder", s.EmbedderSettings)
    assert (embedder.served_model, embedder.base_url) == ("qwen-embed", "http://127.0.0.1:8002/v1")
    reranker = s.load(cfg, "reranker", s.RerankerSettings)
    assert (reranker.served_model, reranker.base_url) == ("qwen-rerank", "http://127.0.0.1:8003")
    assert cfg.qa.llm == "openai/qwen-local" and cfg.ocr.served_model == "qwen-local"
    assert not Path(cfg.output_dir).is_absolute() and not Path(index.dir).is_absolute()


@pytest.mark.parametrize("path", [*EXAMPLES, CONFIG / "pipeline.toml"], ids=lambda p: p.name)
def test_shipped_configs_hold_no_absolute_path_of_a_machine(path):
    assert not re.search(r"/(Users|home|mnt)/|[A-Za-z]:\\", path.read_text())


def test_the_lab_server_example_talks_to_this_machine_only():
    text = (CONFIG / "examples" / "lab-server.toml").read_text()
    assert set(re.findall(r"https?://([^/:\s\"']+)", text)) == {"127.0.0.1"}
    assert "@" not in text  # no address, no key
