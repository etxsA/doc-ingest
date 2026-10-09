"""`docingest index` through the real CLI, store, entry points, embedder client and local index.

Only the embedding server is scripted (httpx.MockTransport); everything else is the code a
user runs, on a temporary folder."""

import json
import re

import pytest
from docingest.bootstrap import Container
from docingest.config import load_config
from docingest.domain.errors import IndexMismatchError
from docingest.entrypoints.cli import app
from servers import EmbeddingServer
from typer.testing import CliRunner

from docingest_index import factories
from docingest_index.embedder import OpenAICompatibleEmbedder

ATTENTION = "Attention weights are computed with a softmax over scaled dot products. " * 30
QUBITS = "Superconducting qubits decohere when they couple to their environment. " * 30
WIDE = {"COLUMNS": "200"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    server = EmbeddingServer()
    monkeypatch.setattr(
        factories,
        "OpenAICompatibleEmbedder",
        lambda s: OpenAICompatibleEmbedder(s, transport=server.transport),
    )
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "attention.md").write_text("# Attention\n" + ATTENTION)
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        f"""output_dir = "{tmp_path / "out"}"
[adapters]
embedder = "openai-compatible"
index = "local"
[index]
dir = "{tmp_path / "index"}"
[embedder]
model = "test/embed"
revision = "{"a" * 40}"
"""
    )
    runner = CliRunner(env=WIDE)
    assert runner.invoke(app, ["ingest", str(raw), "-c", str(cfg)]).exit_code == 0
    return runner, cfg, server, raw


def embedded_chunks(server):
    return sum(len(b["input"]) for b in server.bodies if b["encoding_format"] == "base64")


def test_build_is_incremental_and_the_index_answers_questions(setup, tmp_path):
    runner, cfg, server, raw = setup
    first = runner.invoke(app, ["index", "build", "-c", str(cfg)])
    assert first.exit_code == 0, first.output
    n = embedded_chunks(server)
    assert (
        n > 1 and f"added 1, updated 0, unchanged 0, pruned 0; embedded {n} chunks" in first.output
    )

    again = runner.invoke(app, ["index", "build", "-c", str(cfg)])
    assert "unchanged 1" in again.output and "nothing to commit" in again.output
    assert embedded_chunks(server) == n  # a second run makes no embedding request

    (raw / "qubits.md").write_text("# Qubits\n" + QUBITS)
    assert runner.invoke(app, ["ingest", str(raw), "-c", str(cfg)]).exit_code == 0
    third = runner.invoke(app, ["index", "build", "-c", str(cfg)])
    assert "added 1, updated 0, unchanged 1" in third.output
    new = embedded_chunks(server) - n
    assert 0 < new < n * 2 and f"embedded {new} chunks" in third.output

    container = Container(load_config(cfg), log=lambda _: None)
    embedder, index = container.adapter("embedder"), container.adapter("index")
    question = "superconducting qubits decohere environment"
    hits = index.search(question, embedder.embed_query(question), 5)
    assert hits and "qubits" in hits[0].chunk.text and hits[0].chunk.name.endswith("pages 1-1")
    assert {h.chunk.doc_id for h in hits} == set(index.keys())  # both papers are searchable
    [folder] = (tmp_path / "index").iterdir()
    assert json.loads((folder / "fingerprint.json").read_text())["model"] == "test/embed"


def test_status_and_remove(setup):
    runner, cfg, server, _ = setup
    status = runner.invoke(app, ["index", "status", "-c", str(cfg)])
    assert status.exit_code == 0 and re.search(r"to add\s*│\s*1", status.output)
    assert server.requests == []  # status never calls the embedder
    runner.invoke(app, ["index", "build", "-c", str(cfg)])
    status = runner.invoke(app, ["index", "status", "-c", str(cfg)])
    assert re.search(r"up to date\s*│\s*1", status.output) and "never" not in status.output
    doc_id = next(iter(Container(load_config(cfg)).adapter("index").keys()))
    removed = runner.invoke(app, ["index", "remove", doc_id[:8], "-c", str(cfg)])
    assert removed.exit_code == 0 and f"removed {doc_id}" in removed.output
    assert re.search(
        r"to add\s*│\s*1", runner.invoke(app, ["index", "status", "-c", str(cfg)]).output
    )


def test_an_embedder_other_than_the_index_was_built_for_is_refused(setup, tmp_path):
    runner, cfg, server, _ = setup
    runner.invoke(app, ["index", "build", "-c", str(cfg)])
    requests = len(server.requests)
    other = tmp_path / "other.toml"
    other.write_text(cfg.read_text().replace("a" * 40, "b" * 40))
    # a new revision is a new folder, so the index is empty and simply built again ...
    assert "added 1" in runner.invoke(app, ["index", "build", "-c", str(other)]).output
    assert len(server.requests) > requests
    # ... but the service refuses an index that reports another embedder than the configured one
    container = Container(load_config(cfg), log=lambda _: None)
    index = container.adapter("index")
    index.embedder_fingerprint = "someone else"
    result = Container(
        load_config(cfg), log=lambda _: None, overrides={"index": index}
    ).index_service
    with pytest.raises(IndexMismatchError, match="someone else"):
        result.build()


def test_building_one_corpus_never_prunes_the_index_of_another(setup, tmp_path):
    runner, cfg, *_ = setup
    assert runner.invoke(app, ["index", "build", "-c", str(cfg)]).exit_code == 0
    other_raw = tmp_path / "other-raw"
    other_raw.mkdir()
    (other_raw / "qubits.md").write_text("# Qubits\n" + QUBITS)
    other = tmp_path / "other.toml"  # another corpus (output_dir), the same [index] dir
    other.write_text(cfg.read_text().replace(str(tmp_path / "out"), str(tmp_path / "out-other")))
    assert runner.invoke(app, ["ingest", str(other_raw), "-c", str(other)]).exit_code == 0
    result = runner.invoke(app, ["index", "build", "-c", str(other)])
    assert "added 1, updated 0, unchanged 0, pruned 0" in result.output
    mine = Container(load_config(cfg), log=lambda _: None).adapter("index")
    theirs = Container(load_config(other), log=lambda _: None).adapter("index")
    assert mine.folder != theirs.folder and len(mine.keys()) == len(theirs.keys()) == 1
    assert set(mine.keys()) != set(theirs.keys())
    again = runner.invoke(app, ["index", "build", "-c", str(cfg)])
    assert "unchanged 1" in again.output and "pruned 0" in again.output
