"""``ingest --index`` and ``crawl --index``: new papers reach the chunk index, off by default.

Fakes for the embedder, the index and the crawler, registered as plugins: no model, no network."""

import json
import re
from pathlib import Path

import pytest
from builders import LONG, one_line
from fakes import FakeEmbedder, FakeIndex, FakeOcr, record
from typer.testing import CliRunner

from docingest.domain.errors import RateLimitedError
from docingest.entrypoints.cli import app
from docingest.ports import FetchedSource

WIDE = {"COLUMNS": "200"}


class MarkdownCrawler:
    """Two records; each is fetched as a Markdown file, which the passthrough converter takes."""

    def __init__(self):
        self.records = [record("2401.00001"), record("2401.00002")]

    def search(self, query, limit):
        return self.records[:limit]

    def fetch(self, rec, dest_dir: Path):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{rec.key}.md"
        path.write_text(f"# {rec.key}\n" + LONG.replace("transformer", rec.key))
        return FetchedSource(path=path, record=rec, format="text")


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
        "crawler": MarkdownCrawler(),
        "ocr": FakeOcr(),
    }
    eps = {f"docingest.{port}": EP(obj) for port, obj in parts.items()}
    monkeypatch.setattr(
        "docingest.bootstrap.entry_points", lambda group: [eps[group]] if group in eps else []
    )
    return parts


@pytest.fixture
def setup(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a_note.md").write_text("# Note\n" + LONG)
    (raw / "b_note.txt").write_text(LONG.replace("transformer", "qubit"))
    out = tmp_path / "out"
    cfg = tmp_path / "cfg.toml"
    cfg.write_text(
        f'output_dir = "{out}"\nraw_dir = "{tmp_path / "raw-crawl"}"\n'
        '[adapters]\nembedder = "fake"\nindex = "fake"\ncrawler = "fake"\n'
    )
    plain = tmp_path / "plain.toml"
    plain.write_text(f'output_dir = "{out}"\nraw_dir = "{tmp_path / "raw-crawl"}"\n')
    return CliRunner(env=WIDE), cfg, plain, raw, out


def stored(out):
    return sorted(p.parent.name for p in out.glob("*/manifest.json"))


def test_ingest_does_not_touch_the_index_unless_asked(setup, parts):
    runner, cfg, _, raw, _ = setup
    result = runner.invoke(app, ["ingest", str(raw), "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert parts["embedder"].calls == 0 and parts["index"].keys() == {}
    assert "index:" not in result.output


def test_ingest_index_adds_the_new_papers_and_makes_them_searchable(setup, parts):
    runner, cfg, _, raw, out = setup
    result = runner.invoke(app, ["ingest", str(raw), "--index", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert len(parts["index"].keys()) == 2 == len(stored(out))
    assert parts["index"].stats().searchable_documents == 2
    assert "index: added 2, updated 0, unchanged 0" in result.output
    assert parts["index"].closed >= 1

    calls = parts["embedder"].calls
    again = runner.invoke(app, ["ingest", str(raw), "--index", "-c", str(cfg)])
    assert again.exit_code == 0 and "index: added 0, updated 0, unchanged 2" in again.output
    assert parts["embedder"].calls == calls  # nothing new, nothing embedded


def two_page_pdf(path: Path) -> Path:
    import pypdfium2 as pdfium
    from builders import text_pdf

    first = text_pdf(path.with_name("p1.pdf"), LONG, "Page one is about attention heads.")
    second = text_pdf(path.with_name("p2.pdf"), LONG.replace("transformer", "qubit"), "Page two.")
    doc = pdfium.PdfDocument(str(first))
    doc.import_pages(pdfium.PdfDocument(str(second)))
    doc.save(str(path))
    return path


def test_ingest_index_max_pages_is_satisfied_by_the_canonical_result(setup, parts, tmp_path):
    # Documents that a complete canonical result satisfies `--max-pages`: no variant is stored,
    # so this does not exercise the variant case (see the --ocr-all test below).
    runner, cfg, _, _, out = setup
    pdf = two_page_pdf(tmp_path / "paper.pdf")
    assert runner.invoke(app, ["ingest", str(pdf), "--index", "-c", str(cfg)]).exit_code == 0
    before = dict(parts["index"].keys())
    chunks = parts["index"].stats().chunks
    [manifest] = (out / next(iter(before))[:16]).glob("manifest.json")
    assert len(before) == 1 and json.loads(manifest.read_text())["n_pages"] == 2
    result = runner.invoke(app, ["ingest", str(pdf), "--max-pages", "1", "--index", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "unchanged 1" in result.output  # the corpus serves the complete paper
    assert parts["index"].keys() == before and parts["index"].stats().chunks == chunks
    status = runner.invoke(app, ["index", "status", "-c", str(cfg)])
    assert re.search(r"up to date\s*│\s*1", status.output)


def test_ingest_index_of_a_forced_ocr_variant_keeps_the_version_the_corpus_serves(
    setup, parts, tmp_path
):
    """`--ocr-all` stores a variant beside the canonical result; the corpus keeps serving the
    canonical one, so the index must not switch to the variant."""
    runner, cfg, _, _, out = setup
    ocr_cfg = cfg.with_name("ocr.toml")
    ocr_cfg.write_text(cfg.read_text() + 'ocr = "fake"\n')
    pdf = two_page_pdf(tmp_path / "paper.pdf")
    assert runner.invoke(app, ["ingest", str(pdf), "--index", "-c", str(ocr_cfg)]).exit_code == 0
    before = dict(parts["index"].keys())
    assert len(before) == 1
    result = runner.invoke(app, ["ingest", str(pdf), "--ocr-all", "--index", "-c", str(ocr_cfg)])
    assert result.exit_code == 0, result.output
    assert parts["ocr"].calls == 2  # every page was read by the OCR engine
    assert len(list((out / "_variants").glob("*/manifest.json"))) == 1  # the variant is stored
    assert "unchanged 1" in result.output
    assert parts["index"].keys() == before


def test_ingest_index_adds_only_what_this_run_stored(setup, parts):
    runner, cfg, _, raw, _ = setup
    runner.invoke(app, ["ingest", str(raw / "a_note.md"), "-c", str(cfg)])  # not indexed
    result = runner.invoke(app, ["ingest", str(raw / "b_note.txt"), "--index", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert len(parts["index"].keys()) == 1  # the earlier paper waits for `index build`
    assert "added 1" in result.output


def test_ingest_index_without_an_index_fails_before_ingesting_anything(setup, parts):
    runner, _, plain, raw, out = setup
    result = runner.invoke(app, ["ingest", str(raw), "--index", "-c", str(plain)])
    assert result.exit_code == 2 and "--index needs [adapters] index and embedder" in one_line(
        result.output
    )
    assert stored(out) == []


def test_ingest_index_with_an_index_of_another_embedder_fails_before_ingesting(setup, parts):
    runner, cfg, _, raw, out = setup
    parts["index"].embedder_fingerprint = "another model"
    result = runner.invoke(app, ["ingest", str(raw), "--index", "-c", str(cfg)])
    assert result.exit_code == 1 and "another model" in result.output
    assert stored(out) == [] and parts["embedder"].calls == 0


def test_a_failing_index_update_keeps_the_papers_and_says_how_to_catch_up(setup, parts):
    runner, cfg, _, raw, out = setup

    def down(texts):
        raise RuntimeError("the embedding server went away")

    parts["embedder"].embed_documents = down
    result = runner.invoke(app, ["ingest", str(raw), "--index", "-c", str(cfg)])
    assert result.exit_code == 1
    assert "the index was not updated: the embedding server went away" in result.output
    assert "docingest index build" in result.output
    assert len(stored(out)) == 2  # the ingestion itself is not undone
    assert parts["index"].closed >= 1


def test_files_that_fail_to_ingest_still_leave_the_good_ones_indexed(setup, parts):
    runner, cfg, _, raw, _ = setup
    (raw / "broken.xyz").write_bytes(b"\x00\x01")  # unsupported: a failed input
    result = runner.invoke(app, ["ingest", str(raw), "--index", "-c", str(cfg)])
    assert result.exit_code == 1 and "Failed inputs" in result.output
    assert len(parts["index"].keys()) == 2


def test_crawl_index_adds_the_papers_it_ingested(setup, parts):
    runner, cfg, _, _, out = setup
    result = runner.invoke(app, ["crawl", "cat:cs.CL", "--index", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert len(stored(out)) == 2 and len(parts["index"].keys()) == 2
    assert "index: added 2" in result.output


def test_crawl_does_not_touch_the_index_unless_asked(setup, parts):
    runner, cfg, *_ = setup
    assert runner.invoke(app, ["crawl", "cat:cs.CL", "-c", str(cfg)]).exit_code == 0
    assert parts["index"].keys() == {} and parts["embedder"].calls == 0


def test_crawl_index_needs_papers_to_ingest_and_an_index(setup, parts):
    runner, cfg, plain, _, out = setup
    nothing = runner.invoke(app, ["crawl", "cat:cs.CL", "--no-ingest", "--index", "-c", str(cfg)])
    assert nothing.exit_code == 2 and "nothing to add with --no-ingest" in one_line(nothing.output)
    no_index = runner.invoke(app, ["crawl", "cat:cs.CL", "--index", "-c", str(plain)])
    assert no_index.exit_code == 2 and "--index needs [adapters] index" in one_line(no_index.output)
    assert stored(out) == [] and not (out.parent / "raw-crawl").exists()


def test_a_crawl_that_stops_early_still_indexes_what_it_ingested(setup, parts):
    runner, cfg, *_ = setup
    crawler = parts["crawler"]
    original = crawler.fetch

    def fetch(rec, dest_dir):
        if rec.key == "2401.00002":
            raise RateLimitedError("slow down")
        return original(rec, dest_dir)

    crawler.fetch = fetch
    result = runner.invoke(app, ["crawl", "cat:cs.CL", "--index", "-c", str(cfg)])
    assert result.exit_code == 1 and "crawl stopped early" in result.output
    assert len(parts["index"].keys()) == 1 and "index: added 1" in result.output
