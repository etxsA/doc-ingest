"""Benchmark suite adapters (synthetic scans, olmOCR-bench layout + scorer parsing) and the
``docingest bench`` sub-app end to end with a fake OCR adapter."""

import json
import shutil
import sys
from pathlib import Path

import pytest
from builders import LONG, blank_pdf, text_pdf
from fakes import FakeOcr
from typer.testing import CliRunner

from docingest.adapters.datasets.olmocr_bench import (
    CATEGORIES,
    OlmOcrBenchSuite,
    ScorerConfig,
    failed_test_ids,
    parse_scorer_stdout,
    prepare_subset,
    render_first_page,
)
from docingest.adapters.datasets.olmocr_bench import SCORING_VERSION as OLMOCR_SCORING_VERSION
from docingest.adapters.datasets.synthetic import SCORING_VERSION as SYNTHETIC_SCORING_VERSION
from docingest.adapters.datasets.synthetic import SyntheticSuite, plain_text
from docingest.domain.text import clean_text_layer
from docingest.ports import BenchmarkSuite

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bench"
STDOUT = FIXTURES / "scorer_stdout.txt"


# --------------------------------------------------------------------------- synthetic


@pytest.fixture
def two_page_source(tmp_path):
    """A text page (good reference) + a blank page (skipped: no reference)."""
    import pypdfium2 as pdfium

    src = text_pdf(tmp_path / "text.pdf", LONG, LONG, LONG)
    doc = pdfium.PdfDocument(src)
    doc.import_pages(pdfium.PdfDocument(blank_pdf(tmp_path / "blank.pdf", 1)))
    doc.save(tmp_path / "src.pdf")
    return tmp_path / "src.pdf"


def test_synthetic_suite_builds_deterministic_samples(two_page_source):
    suite = SyntheticSuite([(two_page_source, [0, 1, 7])], dpi=100, min_ref_chars=100)
    assert isinstance(suite, BenchmarkSuite)
    samples = suite.samples()
    assert [s.id for s in samples] == ["src_p001_clean", "src_p001_light", "src_p001_heavy"]
    assert [s.category for s in samples] == ["clean", "light", "heavy"]
    assert len(suite.skipped) == 2  # blank page 1 (no text), page 7 (out of range)
    raw_ref = samples[0].reference
    assert raw_ref and "transformer" in raw_ref and raw_ref == clean_text_layer(raw_ref)

    again = SyntheticSuite([(two_page_source, [0, 1, 7])], dpi=100, min_ref_chars=100)
    assert again.fingerprint == suite.fingerprint
    for a, b in zip(samples, again.samples(), strict=True):
        assert a.load_image().tobytes() == b.load_image().tobytes()  # seeded degradation
    clean, light, heavy = (s.load_image() for s in samples)
    assert clean.mode == "RGB" and heavy.mode == "L" and clean.size == heavy.size
    assert clean.size == (850, 1100)  # 8.5 x 11 in at 100 dpi
    assert light.tobytes() != heavy.convert("L").tobytes()
    other = SyntheticSuite([(two_page_source, [0])], dpi=150, min_ref_chars=100)
    assert other.fingerprint != suite.fingerprint


def test_synthetic_suite_scores_outputs_per_level(two_page_source, tmp_path):
    suite = SyntheticSuite([(two_page_source, [0])], levels=["clean", "heavy"], min_ref_chars=100)
    clean, heavy = suite.samples()
    run = tmp_path / "run"
    for s, text in ((clean, clean.reference), (heavy, "")):  # empty output: CER 1
        path = suite.output_path(run, "cand", s)
        assert path == run / "synthetic" / "cand" / f"{s.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "")
    sc = suite.score(run, ["cand", "absent"])
    cand = sc["cand"]
    assert cand.primary == "cer" and not cand.higher_is_better and not cand.errors
    assert cand.by_category["clean"]["cer"].mean == 0.0
    assert cand.by_category["heavy"]["cer"].mean == 1.0
    assert cand.metrics["cer"].mean == 0.5 and cand.metrics["cer"].n == 2
    assert cand.units[clean.id]["word_f1"] == 1.0
    assert sc["absent"].metrics["cer"].mean is None and sc["absent"].errors


@pytest.fixture
def paper_with_furniture(tmp_path):
    """Four pages: a running header on top, a page number at the bottom, like a paper."""
    import pypdfium2 as pdfium

    header = "Language Agents Achieve Superhuman Synthesis"
    bodies = [
        "Retrieval augmented agents answer questions about the scientific literature.",
        "They cite their sources and refuse to answer when evidence is missing.",
        "Contradiction detection compares every claim against related papers.",
        "Human experts were outperformed on precision while matching recall.",
    ]
    doc = pdfium.PdfDocument.new()
    for i, body in enumerate(bodies):
        page = text_pdf(tmp_path / f"page{i}.pdf", header, body, str(i + 1))
        doc.import_pages(pdfium.PdfDocument(page))
    doc.save(tmp_path / "paper.pdf")
    return tmp_path / "paper.pdf", header, bodies


def test_synthetic_scoring_strips_page_furniture_and_clusters_levels(
    paper_with_furniture, tmp_path
):
    src, header, bodies = paper_with_furniture
    suite = SyntheticSuite([(src, [0, 1, 2, 3])], levels=["clean", "heavy"], min_ref_chars=40)
    samples = suite.samples()
    assert len(samples) == 8 and header in (samples[0].reference or "")  # samples unchanged
    run = tmp_path / "run"
    for s in samples:  # follows "omit page headers/footers": only the body
        body = bodies[int(s.extra["page"])]
        for cand, text in (("follows", body), ("copies", s.reference or "")):
            out = suite.output_path(run, cand, s)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
    scores = suite.score(run, ["follows", "copies"])
    # Old: the prompt-following candidate lost ~0.4 CER per page to header + page number.
    for sc in scores.values():
        assert sc.metrics["cer"].mean == 0.0 and sc.scoring_version == SYNTHETIC_SCORING_VERSION
    follows = scores["follows"]
    assert follows.unit_clusters["paper_p001_clean"] == follows.unit_clusters["paper_p001_heavy"]
    assert follows.details["n_clusters"] == 4 and len(set(follows.unit_clusters.values())) == 4


def test_plain_text_drops_markup_a_text_layer_cannot_contain():
    md = (
        "<!-- Table tag --><table><tr><td colspan='3'>BLEU &amp; F1</td></tr></table>\n"
        "![a chart of losses](page_1_2_3_4.png) <page_number>7</page_number> x < y > z"
    )
    out = " ".join(plain_text(md).split())
    assert out == "BLEU & F1 7 x < y > z"


def test_synthetic_suite_rejects_bad_settings(two_page_source, tmp_path):
    with pytest.raises(ValueError, match="levels"):
        SyntheticSuite([(two_page_source, [0])], levels=["blurry"])
    with pytest.raises(ValueError, match="primary_metric"):
        SyntheticSuite([(two_page_source, [0])], primary_metric="bleu")
    from docingest.domain.errors import DocumentOpenError

    with pytest.raises(DocumentOpenError):
        SyntheticSuite([(tmp_path / "missing.pdf", [0])])


# --------------------------------------------------------------------------- olmOCR-bench


@pytest.fixture
def subset(tmp_path):
    """The 7-PDF smoke subset (one per category): real jsonl files, PDFs not needed."""
    dst = tmp_path / "subset"
    shutil.copytree(FIXTURES / "olmocr_subset", dst)
    return dst


def test_olmocr_layout_matches_the_official_scorer(subset, tmp_path):
    suite = OlmOcrBenchSuite(subset)
    samples = suite.samples()
    assert len(samples) == 7 and {s.category for s in samples} == set(CATEGORIES)
    table = next(s for s in samples if s.category == "table_tests")
    assert table.id == "tables/3b18f8c75b5f8cae89fa5b0cf094949966d4_pg2_pg1"
    run = tmp_path / "run"
    assert suite.output_path(run, "cand", table) == (
        run / "olmocr-bench" / "cand" / "tables"
        / "3b18f8c75b5f8cae89fa5b0cf094949966d4_pg2_pg1_pg1_repeat1.md"
    )  # fmt: skip
    tests = suite.tests()
    assert len(tests) == 56 and sum(t.group == "baseline" for t in tests) == 7
    assert "old_scans_math/3_pg39.pdf_baseline" in {t.id for t in tests}
    view = suite.scorer_dir(run)
    assert view == run / "olmocr-bench"
    assert sorted(p.name for p in view.glob("*.jsonl")) == sorted(f"{c}.jsonl" for c in CATEGORIES)
    assert (view / "pdfs").is_symlink() and (view / "pdfs").resolve() == (subset / "pdfs").resolve()
    suite.scorer_dir(run)  # idempotent
    skip = OlmOcrBenchSuite(subset, scorer=ScorerConfig(Path("py"), skip_baseline=True))
    assert len(skip.tests()) == 49
    # Settings that change the scores are stamped on them (a change re-scores); paths not.
    moved = OlmOcrBenchSuite(subset, scorer=ScorerConfig(Path("elsewhere/py"), timeout_s=5))
    assert (
        moved.scoring_options
        == OlmOcrBenchSuite(subset, scorer=ScorerConfig(Path("py"))).scoring_options
    )
    assert skip.scoring_options["skip_baseline"] is True


def test_scorer_stdout_parser_on_captured_output(subset):
    res = parse_scorer_stdout(STDOUT.read_text())
    assert set(res) == {"empty", "partial", "textlayer"}
    t = res["textlayer"]
    assert (t.overall, t.half_width, t.ci_low, t.ci_high, t.n_tests) == (
        0.196,
        0.097,
        0.098,
        0.292,
        56,
    )
    assert t.by_jsonl["old_scans"] == (2, 6) and t.by_jsonl["baseline"] == (4, 7)
    assert t.by_type["math"] == (0.0, 27) and len(t.by_jsonl) == 8 and not t.errors
    assert res["empty"].overall == 0.167 and res["empty"].by_jsonl["headers_footers"] == (1, 1)
    p = res["partial"]
    assert p.overall is None and len(p.errors) == 6 and "missing MD repeats" in p.errors[0]

    known = {x.id for x in OlmOcrBenchSuite(subset).tests()}
    failed = failed_test_ids(t.failed, known)
    assert failed <= known and len(failed) == len(t.failed)
    passed = 56 - len(failed)
    assert passed == sum(p for p, _ in t.by_jsonl.values())


def test_olmocr_score_runs_the_scorer_and_builds_per_test_units(subset, tmp_path):
    fake_python = tmp_path / "fake-python"  # stands in for .bench-venv/bin/python
    fake_python.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if sys.argv[1] == '-c':\n    print('0.4.27')\n    raise SystemExit\n"
        f"assert sys.argv[1:4] == ['-m', 'olmocr.bench.benchmark', '--dir']\n"
        f"print(open({str(STDOUT)!r}).read())\n"
    )
    fake_python.chmod(0o755)
    suite = OlmOcrBenchSuite(subset, scorer=ScorerConfig(fake_python), log=lambda _: None)
    run = tmp_path / "run"
    for cand in ("textlayer", "partial"):
        for s in suite.samples():
            out = suite.output_path(run, cand, s)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("text")
    scores = suite.score(run, ["textlayer", "partial", "notrun"])

    t = scores["textlayer"]
    assert t.primary == "pass_rate" and t.higher_is_better and not t.errors
    # The pass rate is the scorer's; the CI resamples PDFs (ours), the scorer's is kept.
    est = t.metrics["pass_rate"]
    assert est.mean == 0.196 and est.low is not None and est.high is not None
    assert est.low < 0.196 < est.high and (est.low, est.high) != (0.098, 0.292)
    assert t.details["official_ci"] == [0.098, 0.292] and t.details["n_clusters"] == 7
    assert t.scoring_version == OLMOCR_SCORING_VERSION
    assert scores["notrun"].scoring_version == OLMOCR_SCORING_VERSION
    assert t.by_category["old_scans"]["pass_rate"].mean == pytest.approx(2 / 6, abs=1e-4)
    assert len(t.units) == 56 and set(t.unit_groups.values()) == {*CATEGORIES, "baseline"}
    assert t.details["warnings"] == [] and t.details["olmocr"] == "0.4.27"
    assert (run / t.details["log"]).read_text().startswith("\nRunning tests")
    assert "scorer failed" in scores["partial"].errors[0]
    assert "missing MD repeats" in scores["partial"].errors[1]
    assert "no output yet" in scores["notrun"].errors[0]

    missing = OlmOcrBenchSuite(subset, scorer=ScorerConfig(tmp_path / "nope"), log=print)
    assert "not installed" in missing.score(run, ["textlayer"])["textlayer"].errors[0]


def test_render_first_page_long_side(tmp_path):
    img = render_first_page(text_pdf(tmp_path / "p.pdf", "hello"), 2048)
    assert img.height == 2048 and abs(img.width - 1584) <= 1 and img.mode == "RGB"


@pytest.mark.network
def test_prepare_subset_downloads_a_seeded_nested_sample(tmp_path):
    rev = json.loads((FIXTURES / "olmocr_subset" / "subset.json").read_text())["revision"]
    one = prepare_subset(tmp_path, revision=rev, categories=["old_scans_math"], per_category=1)
    two = prepare_subset(tmp_path, revision=rev, categories=["old_scans_math"], per_category=2)
    pdfs1 = json.loads((one / "subset.json").read_text())["pdfs"]
    pdfs2 = json.loads((two / "subset.json").read_text())["pdfs"]
    assert len(pdfs1) == 1 and len(pdfs2) == 2 and set(pdfs1) <= set(pdfs2)
    for rel in pdfs2:
        assert (two / "pdfs" / rel).is_file()
    assert (
        prepare_subset(tmp_path, revision=rev, categories=["old_scans_math"], per_category=1) == one
    )


# --------------------------------------------------------------------------- CLI


def test_bench_cli_run_resume_report(tmp_path, monkeypatch, two_page_source):
    from docingest import bootstrap
    from docingest.entrypoints.bench_cli import bench_app

    engines = []

    def fake_ocr(cfg):
        engines.append(cfg.ocr)
        return FakeOcr(text="Attention is all you need", model=cfg.ocr.repo_id)

    monkeypatch.setitem(bootstrap.REGISTRY["ocr"], "fake", fake_ocr)
    config = tmp_path / "benchmark.toml"
    config.write_text(
        f'runs_dir = "{tmp_path / "runs"}"\ndata_dir = "{tmp_path / "data"}"\n'
        "[suites.synthetic]\nlevels = ['clean']\nmin_ref_chars = 100\n"
        f"documents = [{{ path = '{two_page_source}', pages = [0] }}]\n"
        "[presets.tiny]\ncandidates = ['a']\n"
        "[[candidates]]\nname = 'a'\nrepo_id = 'org/a'\nrevision = 'aaaa'\nocr = 'fake'\n"
        "max_side = 900\n"
        "[[candidates]]\nname = 'b'\nrepo_id = 'org/b'\nrevision = 'bbbb'\nocr = 'fake'\n"
        "profile = 'olmocr'\n"
    )
    cli = CliRunner()
    args = ["--run-id", "t", "--suite", "synthetic", "-c", str(config)]
    res = cli.invoke(bench_app, ["run", *args, "--preset", "tiny"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert [(o.repo_id, o.max_side, o.profile) for o in engines] == [("org/a", 900, "markdown")]
    res = cli.invoke(bench_app, ["run", *args], catch_exceptions=False)  # 'a' resumes, 'b' runs
    assert res.exit_code == 0, res.output
    assert [o.repo_id for o in engines] == ["org/a", "org/b"]
    run_dir = tmp_path / "runs" / "t"
    assert (run_dir / "synthetic" / "b" / "src_p001_clean.md").read_text() == (
        "Attention is all you need"
    )
    res = cli.invoke(
        bench_app, ["report", "--run-id", "t", "-c", str(config), "--resamples", "200"]
    )
    assert res.exit_code == 0, res.output
    summary = json.loads((run_dir / "summary.json").read_text())
    assert set(summary["suites"]["synthetic"]["scores"]) == {"a", "b"}
    assert "## Suite `synthetic`" in (run_dir / "report.md").read_text()
    # A new [scoring.synthetic] table re-scores on the next report, without --rescore.
    cluster = f"{Path(two_page_source).name}#p001"
    base_config = config.read_text()
    config.write_text(
        base_config + "[[scoring.synthetic.report_separately]]\n"
        f"cluster = '{cluster}'\nreason = 'known bad reference'\n"
    )
    res = cli.invoke(
        bench_app, ["report", "--run-id", "t", "-c", str(config), "--resamples", "200"]
    )
    assert res.exit_code == 0, res.output
    scores = json.loads((run_dir / "summary.json").read_text())["suites"]["synthetic"]["scores"]
    assert all(cluster in s["details"]["reported_separately"] for s in scores.values())
    config.write_text(base_config)
    # Same run id, different suite settings: refused instead of mixing outputs.
    config.write_text(config.read_text().replace("levels = ['clean']", "levels = ['heavy']"))
    res = cli.invoke(bench_app, ["run", *args])
    assert res.exit_code == 2 and "fingerprint" in res.output


def test_repo_benchmark_config_pins_every_candidate():
    from docingest.entrypoints.bench_cli import DEFAULT_BENCH_CONFIG, load_bench_config

    bc, preset = load_bench_config(DEFAULT_BENCH_CONFIG, "smoke")
    specs = bc.specs()
    assert len(specs) >= 9 and all(len(s.revision) == 40 for s in specs.values())
    assert {s.profile for s in specs.values()} == {
        "markdown", "olmocr", "nanonets", "glm-ocr", "paddleocr-vl"
    }  # fmt: skip
    assert preset == ["qwen3-vl-2b", "paddleocr-vl"]
    assert bc.suites.olmocr_bench is not None and bc.suites.olmocr_bench.per_category == 1
    assert len(bc.suites.olmocr_bench.revision) == 40
