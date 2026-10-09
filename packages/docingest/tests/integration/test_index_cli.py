"""``docingest index``: build, status and remove through the real CLI, store and container.

The embedder and the index are fakes registered as plugins, so no model and no network."""

import re

import pytest
from builders import LONG
from fakes import FakeEmbedder, FakeIndex
from typer.testing import CliRunner

from docingest.entrypoints.cli import app

WIDE = {"COLUMNS": "200"}  # keep rich's table cells on one line


@pytest.fixture
def corpus(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a_note.md").write_text("# Note\n" + LONG)
    (raw / "b_note.txt").write_text(LONG.replace("transformer", "qubit"))
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        f'output_dir = "{tmp_path / "out"}"\n[adapters]\nembedder = "fake"\nindex = "fake"\n'
    )
    runner = CliRunner(env=WIDE)
    assert runner.invoke(app, ["ingest", str(raw), "-c", str(cfg)]).exit_code == 0
    return runner, cfg


@pytest.fixture
def fakes(monkeypatch):
    """One embedder and one index shared by every command of a test."""
    embedder = FakeEmbedder()
    index = FakeIndex(embedder_fingerprint=embedder.fingerprint)

    class EP:
        def __init__(self, port, obj):
            self.name, self.value, self._obj = "fake", "tests:fake", obj
            self.port = port

        def load(self):
            return lambda cfg: self._obj

    eps = {"docingest.embedder": EP("embedder", embedder), "docingest.index": EP("index", index)}
    monkeypatch.setattr(
        "docingest.bootstrap.entry_points", lambda group: [eps[group]] if group in eps else []
    )
    return embedder, index


def test_build_status_and_remove_follow_the_corpus(corpus, fakes):
    runner, cfg = corpus
    embedder, index = fakes
    result = runner.invoke(app, ["index", "status", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert re.search(r"to add\s*│\s*2", result.output) and "never" in result.output

    result = runner.invoke(app, ["index", "build", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "added 2, updated 0, unchanged 0, pruned 0" in result.output
    assert len(index.keys()) == 2 and index.stats().searchable_documents == 2

    calls = embedder.calls
    again = runner.invoke(app, ["index", "build", "-c", str(cfg)])
    assert "added 0, updated 0, unchanged 2" in again.output and "nothing to commit" in again.output
    assert embedder.calls == calls

    status = runner.invoke(app, ["index", "status", "-c", str(cfg)])
    assert re.search(r"up to date\s*│\s*2", status.output) and re.search(
        r"to add\s*│\s*0", status.output
    )

    doc_id = sorted(index.keys())[0]
    result = runner.invoke(app, ["index", "remove", doc_id[:10], "-c", str(cfg)])
    assert result.exit_code == 0 and f"removed {doc_id}" in result.output
    assert doc_id not in list(index.keys())


def test_a_failure_is_a_message_and_exit_code_1(corpus, fakes):
    runner, cfg = corpus
    _, index = fakes
    result = runner.invoke(app, ["index", "remove", "f" * 12, "-c", str(cfg)])
    assert result.exit_code == 1 and "not in the index" in result.output
    index.embedder_fingerprint = "another model"
    for command in (["build"], ["status"]):
        result = runner.invoke(app, ["index", *command, "-c", str(cfg)])
        if command == ["build"]:
            assert result.exit_code == 1 and "another model" in result.output
        else:
            assert result.exit_code == 0 and "DIFFERENT" in result.output


def test_without_an_index_adapter_the_commands_name_the_setting(corpus):
    runner, cfg = corpus
    plain = cfg.with_name("plain.toml")
    plain.write_text(cfg.read_text().split("[adapters]")[0])
    result = runner.invoke(app, ["index", "build", "-c", str(plain)])
    assert result.exit_code == 1 and '[adapters] index is "none"' in result.output


def test_an_empty_corpus_is_an_error(tmp_path, fakes):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        f'output_dir = "{tmp_path / "out"}"\n[adapters]\nembedder = "fake"\nindex = "fake"\n'
    )
    result = CliRunner(env=WIDE).invoke(app, ["index", "build", "-c", str(cfg)])
    assert result.exit_code == 1 and "no ingested documents" in result.output
