"""``docingest ask`` through the real CLI and store with a configured chunk index.

The embedder, index, reranker and answerer are fakes registered as plugins: no model and no
network. The index is built by ``docingest index build`` so the whole path is exercised."""

import pytest
from builders import LONG, one_line
from fakes import FakeEmbedder, FakeIndex, FakeQA, FakeReranker
from typer.testing import CliRunner

from docingest.entrypoints.cli import app

WIDE = {"COLUMNS": "200"}


@pytest.fixture
def corpus(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a_note.md").write_text("# Note\n" + LONG)
    (raw / "b_note.txt").write_text(LONG.replace("transformer", "qubit"))
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "out"}"\n[adapters]\nqa = "fake"\n')
    runner = CliRunner(env=WIDE)
    assert runner.invoke(app, ["ingest", str(raw), "-c", str(cfg)]).exit_code == 0
    return runner, cfg


class EP:
    def __init__(self, obj):
        self.name, self.value, self._obj = "fake", "tests:fake", obj

    def load(self):
        return lambda cfg: self._obj


@pytest.fixture
def parts(monkeypatch):
    embedder = FakeEmbedder()
    parts = {
        "embedder": embedder,
        "index": FakeIndex(embedder_fingerprint=embedder.fingerprint),
        "reranker": FakeReranker(),
        "qa": FakeQA(),
    }
    eps = {f"docingest.{port}": EP(obj) for port, obj in parts.items()}
    monkeypatch.setattr(
        "docingest.bootstrap.entry_points", lambda group: [eps[group]] if group in eps else []
    )
    return parts


def with_index(cfg, extra=""):
    out = cfg.with_name("indexed.toml")
    out.write_text(
        cfg.read_text()
        + 'embedder = "fake"\nindex = "fake"\nreranker = "fake"\n'
        + "[index]\ncandidates = 6\ncontexts = 2\n"
        + extra
    )
    return out


def test_without_an_index_ask_gives_the_whole_corpus_to_the_answerer(corpus, parts):
    runner, cfg = corpus
    result = runner.invoke(app, ["ask", "what is attention", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "answer to 'what is attention' from 2 docs" in result.output
    assert parts["qa"].contexts is None and len(parts["qa"].seen) == 2


def test_with_an_index_ask_answers_from_the_retrieved_chunks(corpus, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)
    assert runner.invoke(app, ["index", "build", "-c", str(indexed)]).exit_code == 0
    result = runner.invoke(app, ["ask", "the qubit architecture works well", "-c", str(indexed)])
    assert result.exit_code == 0, result.output
    qa = parts["qa"]
    assert qa.contexts is not None and len(qa.contexts) == 2  # [index] contexts = 2
    assert "qubit" in qa.contexts[0].text and set(qa.markdown) == {""}
    assert "answer to" in result.output


def test_no_index_goes_back_to_the_plain_path(corpus, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)
    runner.invoke(app, ["index", "build", "-c", str(indexed)])
    result = runner.invoke(app, ["ask", "what is attention", "--no-index", "-c", str(indexed)])
    assert result.exit_code == 0, result.output
    assert parts["qa"].contexts is None and len(parts["qa"].seen) == 2


def test_bm25_reaches_the_index_for_the_question(corpus, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)
    runner.invoke(app, ["index", "build", "-c", str(indexed)])
    for mode in ("never", "always", "english"):
        result = runner.invoke(app, ["ask", "attention", "--bm25", mode, "-c", str(indexed)])
        assert result.exit_code == 0, result.output
    runner.invoke(app, ["ask", "attention", "-c", str(indexed)])
    assert parts["index"].keywords == ["never", "always", "english", None]


def test_bm25_needs_an_index_and_a_valid_mode(corpus, parts):
    runner, cfg = corpus
    no_index = runner.invoke(app, ["ask", "q", "--bm25", "never", "-c", str(cfg)])
    assert no_index.exit_code == 2 and "needs a configured index" in one_line(no_index.output)
    indexed = with_index(cfg)
    skipped = runner.invoke(app, ["ask", "q", "--bm25", "never", "--no-index", "-c", str(indexed)])
    assert skipped.exit_code == 2 and "needs a configured index" in one_line(skipped.output)
    bad = runner.invoke(app, ["ask", "q", "--bm25", "sometimes", "-c", str(indexed)])
    assert bad.exit_code == 2
    assert parts["qa"].contexts is None and parts["qa"].seen == []


def test_an_index_that_was_never_built_is_a_message_that_names_the_command(corpus, parts):
    runner, cfg = corpus
    result = runner.invoke(app, ["ask", "q", "-c", str(with_index(cfg))])
    assert result.exit_code == 1
    assert "the index is empty" in result.output and "docingest index build" in result.output
    assert "Traceback" not in result.output and parts["qa"].seen == []


def test_an_index_of_another_embedder_is_a_message(corpus, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)
    runner.invoke(app, ["index", "build", "-c", str(indexed)])
    parts["index"].embedder_fingerprint = "another model"
    result = runner.invoke(app, ["ask", "q", "-c", str(indexed)])
    assert result.exit_code == 1 and "another model" in result.output


def test_an_index_without_an_embedder_is_a_message(corpus, parts):
    runner, cfg = corpus
    broken = cfg.with_name("broken.toml")
    broken.write_text(cfg.read_text() + 'index = "fake"\n')
    result = runner.invoke(app, ["ask", "q", "-c", str(broken)])
    assert result.exit_code == 1 and 'embedder is "none"' in result.output


def test_a_bad_index_table_or_a_misbehaving_reranker_is_a_message_not_a_traceback(corpus, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)
    runner.invoke(app, ["index", "build", "-c", str(indexed)])
    bad = cfg.with_name("bad.toml")
    bad.write_text(indexed.read_text().replace("contexts = 2", "contexts = 0"))
    result = runner.invoke(app, ["ask", "q", "-c", str(bad)])
    assert result.exit_code == 1 and "[index] contexts" in result.output
    parts["reranker"].rerank = lambda question, chunks: [0.0]  # one score for many chunks
    result = runner.invoke(app, ["ask", "q", "-c", str(indexed)])
    assert result.exit_code == 1 and "the reranker returned 1 scores" in result.output
    assert "Traceback" not in result.output and parts["qa"].seen == []


def test_a_failure_of_the_answering_step_keeps_its_traceback_in_index_mode_too(corpus, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)
    runner.invoke(app, ["index", "build", "-c", str(indexed)])

    async def broken(question, documents, warn, contexts=None):
        raise ValueError("No texts to add")  # PaperQA2 or litellm failing, not a retrieval problem

    parts["qa"].ask = broken
    result = runner.invoke(app, ["ask", "q", "-c", str(indexed)])
    assert result.exit_code == 1 and isinstance(result.exception, ValueError)
    assert "error:" not in result.output


def test_an_adapter_table_that_does_not_validate_is_a_message(corpus, monkeypatch, parts):
    runner, cfg = corpus
    indexed = with_index(cfg)

    class BrokenEmbedder(EP):
        def load(self):
            def factory(config):
                raise ValueError("[embedder] base_url: Input should be a valid string")

            return factory

    eps = {f"docingest.{port}": EP(obj) for port, obj in parts.items()}
    eps["docingest.embedder"] = BrokenEmbedder(None)
    monkeypatch.setattr(
        "docingest.bootstrap.entry_points", lambda group: [eps[group]] if group in eps else []
    )
    result = runner.invoke(app, ["ask", "q", "-c", str(indexed)])
    assert result.exit_code == 1 and "[embedder] base_url" in result.output
    assert not isinstance(result.exception, ValueError)


def test_the_plain_path_still_fails_the_way_it_did(tmp_path, parts):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "empty"}"\n[adapters]\nqa = "fake"\n')
    result = CliRunner(env=WIDE).invoke(app, ["ask", "q", "-c", str(cfg)])
    assert result.exit_code == 1 and isinstance(result.exception, RuntimeError)
