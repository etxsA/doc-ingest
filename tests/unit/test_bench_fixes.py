"""Regression tests for the benchmark review: clustered statistics, the OCR retry ladder in
telemetry, page furniture and LaTeX / <img> normalization, and stale saved scores.

Each test names the behaviour the old code got wrong; none loads MLX or a model (the
MLX adapter runs against stub modules).
"""

import json
import sys
import types

import numpy as np
import pytest
from fakes import FakeOcr
from PIL import Image
from test_benchmark import EchoOcr, MemSuite, TaggedSuite, runner, spec

from docingest.application.benchmark import (
    build_summary,
    compare,
    needs_scoring,
    read_telemetry,
    render_report,
    score_run,
    telemetry_path,
    throughput,
)
from docingest.application.metrics import (
    normalize,
    plain_text,
    running_lines,
    score,
    strip_furniture,
)
from docingest.application.stats import cluster_bootstrap_ci, paired_bootstrap
from docingest.ports import Estimate, OcrResult, SuiteScore

# --------------------------------------------------------------------------- clustering


def test_tiny_comparisons_are_never_significant():
    # Old: one pair gave CI [0.1, 0.1], p = 0 and "significant"; three all-positive pairs
    # also p = 0. The exact sign-flip p of k independent clusters is at least 2/2^k.
    one = paired_bootstrap([0.5], [0.4])
    assert one.p_value == 1.0 and not one.significant and one.n_clusters == 1
    three = paired_bootstrap([0.3, 0.2, 0.25], [0.1, 0.15, 0.2])
    assert three.p_value == pytest.approx(0.25) and three.exact and not three.significant


def test_levels_of_one_page_count_as_one_cluster():
    # The smoke run: 2 pages x 2 levels, the candidate worse on all 4 units. The old unit
    # bootstrap said p = 0.0072, "significant"; there are only 2 independent pages.
    a, b = [0.30, 0.34, 0.20, 0.22], [0.05, 0.04, 0.03, 0.05]
    pages = ["p001", "p001", "p010", "p010"]
    units = paired_bootstrap(a, b, n=2000)
    clustered = paired_bootstrap(a, b, clusters=pages, n=2000)
    assert units.low > 0  # the old code called a CI excluding 0 "significant"
    assert clustered.n == 4 and clustered.n_clusters == 2
    assert clustered.p_value == 0.5 and not clustered.significant


def test_clustered_null_keeps_the_nominal_false_positive_rate():
    # 12 pages x 3 levels, page-level effects, true mean difference 0. Treating the 36
    # units as independent rejects far more often than 5%; clustering does not.
    rng = np.random.default_rng(7)
    pages = [f"p{i}" for i in range(12) for _ in range(3)]
    reps, naive, clustered = 120, 0, 0
    for _ in range(reps):
        d = np.repeat(rng.normal(0, 0.05, 12), 3) + rng.normal(0, 0.01, 36)
        a, b = list(0.2 + d), [0.2] * 36
        naive += paired_bootstrap(a, b, n=200).significant
        clustered += paired_bootstrap(a, b, clusters=pages, n=200).significant
    assert naive / reps > 0.15
    assert clustered / reps <= 0.10


def test_cluster_bootstrap_ci_carries_whole_clusters():
    rng = np.random.default_rng(3)
    means = rng.uniform(0, 1, 10)
    values = [float(x) for x in np.repeat(means, 3)]  # every cluster seen 3 times
    clusters = [str(i) for i in range(10) for _ in range(3)]
    m1, lo1, hi1 = cluster_bootstrap_ci(values)
    m2, lo2, hi2 = cluster_bootstrap_ci(values, clusters=clusters)
    m3, lo3, hi3 = cluster_bootstrap_ci([float(x) for x in means])
    assert m1 == pytest.approx(m2) == pytest.approx(m3)
    assert (hi2 - lo2) > 1.4 * (hi1 - lo1)  # ~sqrt(3) wider: 10 clusters, not 30 units
    assert (hi2 - lo2) == pytest.approx(hi3 - lo3, rel=0.15)
    single = cluster_bootstrap_ci([0.2, 0.4], clusters=["x", "x"])
    assert single[0] == pytest.approx(0.3) and np.isnan(single[1]) and np.isnan(single[2])
    with pytest.raises(ValueError, match="aligned"):
        cluster_bootstrap_ci([1.0, 2.0], clusters=["x"])


def test_stratified_clusters_resample_within_strata_and_collapse_singletons():
    # Two strata of 5 PDFs x 2 tests; statistic = mean of per-group means.
    groups = ["g1"] * 10 + ["g2"] * 10
    clusters = [f"pdf{i // 2}" for i in range(20)]
    values = [1.0] * 10 + [0.0, 1.0] * 5  # g1 always passes; g2 PDFs pass half their tests
    mean, lo, hi = cluster_bootstrap_ci(values, groups=groups, clusters=clusters, strata=groups)
    assert mean == lo == hi == pytest.approx(0.75)  # every PDF alike within its stratum
    with pytest.raises(ValueError, match="spans strata"):
        cluster_bootstrap_ci([1.0, 0.0], clusters=["x", "x"], strata=["a", "b"])
    # One PDF per category (the smoke subset): strata are pooled instead of each
    # contributing zero variance.
    _, lo, hi = cluster_bootstrap_ci(
        [1.0, 0.0, 1.0], groups=["a", "b", "c"], clusters=["x", "y", "z"], strata=["a", "b", "c"]
    )
    assert lo < hi


def test_compare_uses_the_suites_clusters():
    def sc(values):
        units = {f"u{i}": {"cer": v} for i, v in enumerate(values)}
        clusters = {f"u{i}": f"page{i // 2}" for i in range(len(values))}
        return SuiteScore("cer", False, {"cer": Estimate(0.1)}, units=units, unit_clusters=clusters)

    r = compare(sc([0.3, 0.3, 0.2, 0.2]), sc([0.1, 0.1, 0.1, 0.1]), n=500)
    assert r.n == 4 and r.n_clusters == 2 and not r.significant


def test_report_shows_clusters_and_warns_when_there_are_few(tmp_path):
    suite = TaggedSuite(ids=[f"s{i}" for i in range(4)])

    class Wrong(EchoOcr):
        def transcribe(self, image):
            return OcrResult("nope", 1.0, 10, "stop", None)

    runner({"good": EchoOcr(), "bad": Wrong()}).run(suite, [spec("good"), spec("bad")], tmp_path)
    score_run(suite, tmp_path)
    md = render_report(build_summary(tmp_path, n_boot=200))
    assert "| pairs | clusters |" in md
    assert "| bad | -1.000 [-1.000, -1.000] | 0.1250 | no | 4 | 4 |" in md
    assert "Fewer than 10 clusters" in md


def test_report_labels_the_official_olmocr_ci_separately(tmp_path):
    runner({"a": EchoOcr()}).run(TaggedSuite(), [spec("a")], tmp_path)
    (tmp_path / "scores").mkdir()
    sc = SuiteScore(
        "pass_rate",
        True,
        {"pass_rate": Estimate(0.196, 0.095, 0.371, 56)},
        details={
            "official_ci": [0.098, 0.292],
            "half_width": 0.097,
            "official_ci_method": "olmocr.bench: tests resampled within each jsonl file",
            "clusters": "a PDF with all its tests, drawn within its category",
            "n_clusters": 7,
        },
    )
    (tmp_path / "scores" / "mem.json").write_text(json.dumps({"a": sc.to_dict()}))
    md = render_report(build_summary(tmp_path, n_boot=100))
    assert "| a | 19.6% [9.5, 37.1] |" in md  # the headline CI is the clustered one
    assert "One cluster = a PDF with all its tests, drawn within its category; 7 clusters." in md
    assert "Official scorer CI" in md and "| a | [9.8, 29.2] | 9.7% |" in md


# --------------------------------------------------------------------------- retry ladder


def _stub_mlx(monkeypatch, results):
    """mlx / mlx_vlm stand-ins: ``generate`` replays ``results``; seeds are recorded."""
    calls, seeds = [], []

    def generate(model, processor, prompt, image, verbose, **kw):
        calls.append(kw)
        return results[len(calls) - 1]

    mlx_vlm = types.ModuleType("mlx_vlm")
    mlx_vlm.generate = generate
    mlx_vlm.apply_chat_template = lambda *a, **k: "prompt"
    core = types.ModuleType("mlx.core")
    core.reset_peak_memory = lambda: None
    core.random = types.SimpleNamespace(seed=seeds.append)
    mlx = types.ModuleType("mlx")
    mlx.core = core
    for name, module in {"mlx": mlx, "mlx.core": core, "mlx_vlm": mlx_vlm}.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls, seeds


def _gen(text, tokens, finish, tps=105.0):
    return types.SimpleNamespace(
        text=text,
        generation_tokens=tokens,
        finish_reason=finish,
        generation_tps=tps,
        peak_memory=3.0,
    )


def test_mlx_engine_reports_the_whole_retry_ladder_and_seeds_sampling(monkeypatch):
    from docingest.adapters.ocr.mlx_vlm import MlxVlmOcr
    from docingest.adapters.ocr.profiles import profile_for
    from docingest.domain.models import ModelRef

    loop, fixed = _gen("loop " * 50, 4096, "length"), _gen("Dear Sir", 92, "stop")
    calls, seeds = _stub_mlx(monkeypatch, [loop, fixed, loop, fixed])
    engine = MlxVlmOcr(ModelRef(repo_id="org/m", revision="0" * 40), profile_for("markdown"))
    engine._model = object()  # "loaded": nothing is read from disk
    page = Image.new("RGB", (64, 64), "white")
    res = engine.transcribe(page)
    # Old: attempts, the first "length" and the 4096 looped tokens were all lost.
    assert res.text == "Dear Sir" and res.finish_reason == "stop" and res.gen_tokens == 92
    assert res.attempts == 2 and res.first_finish_reason == "length"
    assert res.total_gen_tokens == 4188
    assert res.gen_seconds == pytest.approx(4188 / 105.0)
    assert [c["temperature"] for c in calls] == [0.0, 0.2]
    assert len(seeds) == 1  # the greedy first attempt needs no seed; the sampled one does
    engine.transcribe(page)
    assert seeds[1] == seeds[0]  # same page, same sampling: a rerun reproduces the text


def test_openai_engine_reports_the_retry_ladder(monkeypatch):
    from docingest.adapters.ocr.openai_compat import OpenAICompatibleOcr
    from docingest.adapters.ocr.profiles import profile_for
    from docingest.domain.models import ModelRef

    replies = iter(
        [
            {"choices": [{"message": {"content": "loop"}, "finish_reason": "length"}],
             "usage": {"completion_tokens": 4096}},
            {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
             "usage": {"completion_tokens": 20}},
        ]
    )  # fmt: skip
    engine = OpenAICompatibleOcr(
        ModelRef(repo_id="org/m", revision="0" * 40),
        profile_for("markdown"),
        base_url="http://localhost:1",
    )
    monkeypatch.setattr(engine, "_post", lambda body: next(replies))
    res = engine.transcribe(Image.new("RGB", (32, 32), "white"))
    assert (res.text, res.gen_tokens, res.finish_reason) == ("done", 20, "stop")
    assert (res.attempts, res.first_finish_reason, res.total_gen_tokens) == (2, "length", 4116)
    assert res.gen_seconds is None


def test_single_attempt_results_fill_the_ladder_from_the_final_attempt():
    res = OcrResult("x", 1.0, 40, "stop")
    assert (res.attempts, res.first_finish_reason, res.total_gen_tokens) == (1, "stop", 40)
    # An empty page (0 tokens, 0 s of decoding) keeps the exact decode basis.
    recs = [
        {"sample_id": "a", "seconds": 2.0, "gen_tokens": 200, "finish_reason": "stop",
         "attempts": 1, "total_gen_tokens": 200, "gen_seconds": 1.0, "error": None},
        {"sample_id": "b", "seconds": 0.5, "gen_tokens": 0, "finish_reason": "stop",
         "attempts": 1, "total_gen_tokens": 0, "gen_seconds": 0.0, "error": None},
    ]  # fmt: skip
    tp = throughput(recs)
    assert tp["gen_tok_s"] == 200.0 and tp["gen_tok_s_basis"] == "decode"


class RetriedOcr(FakeOcr):
    """Page "b" looped to max_tokens and was fixed by a retry; the rest decode normally."""

    def transcribe(self, image):
        if image.info.get("id") == "b":
            return OcrResult("b", 40.0, 92, "stop", None, 2, "length", 4188, 39.9)
        return OcrResult(image.info.get("id", ""), 10.0, 1000, "stop", None, 1, None, None, 9.5)


def test_runner_records_the_ladder_and_throughput_counts_first_try_truncations(tmp_path):
    runner({"r": RetriedOcr()}).run(TaggedSuite(), [spec("r")], tmp_path)
    recs = read_telemetry(telemetry_path(tmp_path, "mem", "r"))
    b = next(r for r in recs if r["sample_id"] == "b")
    assert (b["attempts"], b["first_finish_reason"], b["total_gen_tokens"]) == (2, "length", 4188)
    tp = throughput(recs)
    # Old: truncation 0% and tok/s = (1000 + 1000 + 92) / 60 s = 34.9.
    assert tp["first_truncation_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert tp["truncation_rate"] == 0.0 and tp["retried_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert tp["gen_tok_s_basis"] == "decode" and not tp["lower_bound"]
    assert tp["gen_tok_s"] == pytest.approx((1000 + 1000 + 4188) / (9.5 + 9.5 + 39.9), abs=0.1)


def test_telemetry_without_the_ladder_is_reported_as_lower_bounds(tmp_path):
    # What the running 'screen' run writes (older runner): no attempts / first finish.
    legacy = [
        {"sample_id": "a", "seconds": 10.0, "gen_tokens": 1000, "finish_reason": "stop",
         "empty": False, "error": None},
        {"sample_id": "b", "seconds": 40.0, "gen_tokens": 92, "finish_reason": "stop",
         "empty": False, "error": None},
    ]  # fmt: skip
    tp = throughput(legacy)
    assert tp["lower_bound"] and tp["retried_rate"] is None
    assert tp["gen_tok_s_basis"] == "legacy" and tp["gen_tok_s"] == pytest.approx(21.8)
    assert tp["first_truncation_rate"] == 0.0 and tp["truncation_rate"] == 0.0
    # The report renders them, marked, instead of failing on the missing fields.
    runner({"good": EchoOcr()}).run(TaggedSuite(), [spec("good")], tmp_path)
    tele = telemetry_path(tmp_path, "mem", "good")
    tele.write_text("".join(json.dumps(r) + "\n" for r in legacy))
    md = render_report(build_summary(tmp_path, n_boot=100))
    assert "| good | 2 | 25.0 | 37.0 | ≥21.8 |" in md
    assert "| ≥0.0% | 0.0% | n/a |" in md and "lower bound" in md


# --------------------------------------------------------------------------- page furniture

PAGE = """Language Agents Achieve Superhuman Synthesis of Scientific Knowledge
Retrieval augmented agents answer questions about the literature.
They cite their sources and refuse when evidence is missing.
arXiv:2409.13740v2 [cs.CL] 26 Sep 2024
7"""
BODY = """Retrieval augmented agents answer questions about the literature.
They cite their sources and refuse when evidence is missing."""
RUNNING = frozenset(
    {normalize("Language Agents Achieve Superhuman Synthesis of Scientific Knowledge")}
)


def test_furniture_is_stripped_from_reference_and_hypothesis():
    # Old: a transcription following "omit page headers/footers" lost CER on every page.
    assert score(PAGE, BODY)["cer"] > 0.3
    ref, hyp = strip_furniture(PAGE, BODY, RUNNING)
    assert ref == BODY and hyp == BODY and score(ref, hyp)["cer"] == 0.0
    # A model that copies the furniture (in Markdown, stamp elsewhere) scores the same.
    copy = "# Language Agents Achieve Superhuman Synthesis of Scientific Knowledge\n"
    copy += "arXiv:2409.13740v2 [cs.CL] 26 Sep 2024\n" + BODY + "\n**7**"
    assert score(*strip_furniture(PAGE, copy, RUNNING))["cer"] == 0.0


def test_furniture_detection_is_conservative():
    # A title equal to the running header, on a page where it is not furniture (page 1
    # prints it differently), stays in the hypothesis.
    page1 = "LANGUAGE AGENTS ACHIEVE SUPERHUMAN\nSYNTHESIS OF SCIENTIFIC KNOWLEDGE\n" + BODY
    hyp = "# Language Agents Achieve Superhuman Synthesis of Scientific Knowledge\n" + BODY
    ref, kept = strip_furniture(page1, hyp, RUNNING)
    assert ref == page1 and kept == hyp
    # Numbers are page numbers only as the first / last line; references are not stamps.
    text = "Table 1\n3\nresults\narXiv preprint arXiv:1607.06450, 2016.\n12"
    assert (
        strip_furniture(text, "", ())[0]
        == "Table 1\n3\nresults\narXiv preprint arXiv:1607.06450, 2016."
    )


def test_running_lines_recur_on_at_least_half_the_pages():
    header = "A Short Running Title"
    pages = [f"{header}\nbody {i}\n{i}" for i in range(4)] + ["Title page\nabstract"] * 2
    assert running_lines(pages) == {normalize(header)}  # 4 of 6 pages; page numbers ignored
    assert running_lines(pages[:1]) == frozenset()  # one page cannot show a running line
    assert running_lines([f"{header}\nx", "y\nz", "w\nv"]) == frozenset()  # 1 of 3


# --------------------------------------------------------------------------- markup / LaTeX


def test_img_descriptions_are_dropped_like_markdown_alt_text():
    caption = "Figure 2: (left) Scaled Dot-Product Attention."
    desc = "Diagram of attention with MatMul, Scale and SoftMax blocks"
    olmocr_style = f"![{desc}](page_1_2_3_4.png)\n\n{caption}"
    nanonets_style = f"<img>{desc}</img>\n\n{caption}"
    assert score(caption, plain_text(olmocr_style))["cer"] == 0.0
    assert score(caption, plain_text(nanonets_style))["cer"] == 0.0  # old: 0.61


@pytest.mark.parametrize(
    ("reference", "latex"),
    [
        ("queries and keys of dimension dk, and values of dimension dv.",
         "queries and keys of dimension $d_k$, and values of dimension $d_v$."),
        ("We used a beam size of 21 and α = 0.3 for both", r"We used a beam size of 21 and $\alpha = 0.3$ for both"),
        ("dmodel = 512", r"\(d_{\text{model}} = 512\)"),
        ("2.3 · 1019", r"$2.3 \cdot 10^{19}$"),
        ("Attention(Q, K, V) = softmax(QKT √dk)V",
         r"$$\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V$$"),
        ("ϵls = 0.1", r"$\epsilon_{ls} = 0.1$"),  # NFKC folds the variant glyphs alike
        ("x ≤ y ≠ z", r"$x \leq y \neq z$"),
    ],
)  # fmt: skip
def test_latex_math_scores_like_the_text_layer(reference, latex):
    assert score(reference, latex)["cer"] == 0.0 and score(reference, latex)["wer"] == 0.0


def test_latex_normalization_is_symmetric_and_leaves_plain_text_alone():
    assert normalize("## **Bold** `x`") == "bold x"
    assert normalize("snake_case") == normalize(r"snake\_case")  # both sides alike
    assert normalize(r"\begin{aligned} a &= b \\ c &= d \end{aligned}") == "a = b c = d"
    assert normalize(r"\log x + \unknowncmd") == "log x + unknowncmd"


# --------------------------------------------------------------------------- stale scores


class FlakySuite(MemSuite):
    """Scores fail until ``fixed`` (the olmOCR scorer venv was missing)."""

    fixed = False

    def score(self, run_dir, candidates):
        out = super().score(run_dir, candidates)
        if not self.fixed:
            for s in out.values():
                s.metrics = {"acc": Estimate(None)}
                s.errors = ["scorer not installed: run scripts/setup_bench_scorer.sh"]
        return out


def test_a_saved_failure_is_rescored_once_the_cause_is_fixed(tmp_path):
    suite = FlakySuite()
    runner({"a": EchoOcr()}).run(TaggedSuite(), [spec("a")], tmp_path)
    score_run(suite, tmp_path)
    # Old: [] forever (scores file newer than the telemetry), the error stayed in reports.
    assert needs_scoring(tmp_path, "mem") == ["a"]
    suite.fixed = True
    score_run(suite, tmp_path, needs_scoring(tmp_path, "mem"))
    assert needs_scoring(tmp_path, "mem") == []


def test_a_resumed_candidate_is_rescored_after_another_one_was_scored(tmp_path):
    suite = TaggedSuite()
    t = [0.0]

    def clock():
        t[0] += 5.0
        return t[0]

    runner({"a": EchoOcr()}, clock=clock).run(suite, [spec("a")], tmp_path, time_budget_s=10)
    runner({"b": EchoOcr()}).run(suite, [spec("b")], tmp_path)
    score_run(suite, tmp_path)  # "a" scored while incomplete
    runner({"a": EchoOcr()}).run(suite, [spec("a")], tmp_path)  # resumed to 3/3
    score_run(suite, tmp_path, ["b"])  # rewrites scores/mem.json after a's telemetry
    assert needs_scoring(tmp_path, "mem") == ["a"]  # old: []
    score_run(suite, tmp_path, ["a"])
    assert needs_scoring(tmp_path, "mem") == []


def test_a_transcription_landing_during_scoring_makes_the_score_stale(tmp_path):
    class SlowSuite(TaggedSuite):
        def score(self, run_dir, candidates):
            out = super().score(run_dir, candidates)
            # the runner appends a record while the (slow) scorer is still running
            with telemetry_path(run_dir, "mem", "a").open("a") as f:
                f.write(json.dumps({"sample_id": "c", "seconds": 1.0, "error": None}) + "\n")
            return out

    runner({"a": EchoOcr()}).run(TaggedSuite(), [spec("a")], tmp_path)
    score_run(SlowSuite(), tmp_path)
    assert needs_scoring(tmp_path, "mem") == ["a"]  # old: [] (scores written afterwards)


def test_scores_from_other_scoring_rules_or_without_a_stamp_are_rescored(tmp_path):
    suite = TaggedSuite()
    runner({"a": EchoOcr()}).run(suite, [spec("a")], tmp_path)
    score_run(suite, tmp_path)
    assert needs_scoring(tmp_path, "mem") == []
    assert needs_scoring(tmp_path, "mem", scoring_version=2) == ["a"]  # saved: None
    path = tmp_path / "scores" / "mem.json"
    saved = json.loads(path.read_text())
    saved["a"].pop("stamp")  # written by the older runner
    path.write_text(json.dumps(saved))
    assert needs_scoring(tmp_path, "mem") == ["a"]


def test_bench_report_knows_each_suites_scoring_version():
    from docingest.adapters.datasets.olmocr_bench import SCORING_VERSION as OLMOCR
    from docingest.adapters.datasets.synthetic import SCORING_VERSION as SYNTHETIC
    from docingest.entrypoints.bench_cli import SCORING_VERSIONS

    assert SCORING_VERSIONS == {"synthetic": SYNTHETIC, "olmocr-bench": OLMOCR}
    assert SYNTHETIC >= 2 and OLMOCR >= 2


# ------------------------------------------ failure-analysis fixes (scoring v3)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "```markdown\n---\nprimary_language: en\nis_rotation_valid: True\n---\n# T\nBody\n```",
            "# T\nBody",
        ),
        ("```markdown\n---\nprimary_language: en\nis_diagram: False\n---\n# T\nBody", "# T\nBody"),
        ("```yaml\nprimary_language: en\nis_table: false\n---\n# T\nx", "# T\nx"),
        ("---\nprimary_language: en\nis_diagram: False\n------\n# T", "# T"),
        ("# Primary language: en\n# Title\nBody", "# Title\nBody"),
        ("```markdown\n---\nprimary_language: en\nis_diagram: False\n---", ""),
    ],
)
def test_olmocr_front_matter_is_stripped_in_every_observed_shape(raw, expected):
    from docingest.adapters.ocr.profiles import PROFILES

    assert PROFILES["olmocr"].postprocess(raw) == expected


def test_normalizer_drops_markup_but_never_page_text():
    from docingest.application.metrics import normalize

    text = (
        "```markdown\nprimary_language: en\n# Title\n"
        "![Figure showing a comparison between an original PDF file\n"
        "See [code](https://github.com/allenai/olmocr) and (https://x.org/y).\n"
        "![alt](fig.png) after image\n<fcel>cell<lcel>\n```"
    )
    assert normalize(text) == "title see code and . after image cell"


def test_pages_reported_separately_leave_headline_and_paired_units(tmp_path):
    from builders import LONG, text_pdf

    from docingest.adapters.datasets.synthetic import SyntheticSuite

    pdf = text_pdf(tmp_path / "d.pdf", LONG, LONG)
    suite = SyntheticSuite([(pdf, [0])], levels=["clean"], min_ref_chars=10)
    (sample,) = suite.samples()
    cluster = suite.cluster(sample)
    suite.headline_exclusions = {cluster: "reference known to be unrepresentative"}
    out = suite.output_path(tmp_path, "c", sample)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("unrelated text")
    sc = suite.score(tmp_path, ["c"])["c"]
    assert sc.units == {} and sc.metrics["cer"].mean is None
    sep = sc.details["reported_separately"][cluster]
    assert sep["reason"].startswith("reference") and sep["metrics"]["cer"]["mean"] > 0.5


def test_refresh_outputs_reapplies_cleanup_backs_up_and_skips_running(tmp_path):
    import json as _json

    from builders import LONG, text_pdf

    from docingest.adapters.datasets.synthetic import SyntheticSuite
    from docingest.entrypoints.bench_cli import refresh_outputs

    suite = SyntheticSuite(
        [(text_pdf(tmp_path / "d.pdf", LONG), [0])], levels=["clean"], min_ref_chars=10
    )
    (sample,) = suite.samples()
    manifest = {
        "suites": {"synthetic": {"candidates": ["done", "running"]}},
        "candidates": {"done": {"profile": "olmocr"}, "running": {"profile": "olmocr"}},
    }
    raw = "```markdown\n---\nprimary_language: en\n---\n# T"
    for cand in ("done", "running"):
        out = suite.output_path(tmp_path, cand, sample)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(raw)
    tel = tmp_path / "telemetry" / "synthetic"
    tel.mkdir(parents=True)
    record = {"sample_id": sample.id, "chars": len(raw), "empty": False}
    (tel / "done.jsonl").write_text(_json.dumps(record) + "\n")  # complete: 1 of 1 samples
    assert refresh_outputs(tmp_path, suite, manifest) == {"done"}
    assert suite.output_path(tmp_path, "done", sample).read_text() == "# T"
    assert suite.output_path(tmp_path, "running", sample).read_text() == raw  # untouched
    backup = (
        tmp_path / "raw_outputs" / suite.output_path(tmp_path, "done", sample).relative_to(tmp_path)
    )
    assert backup.read_text() == raw
    assert _json.loads((tmp_path / "postprocess_log.json").read_text())["synthetic"]["done"]
    (updated,) = [_json.loads(x) for x in (tel / "done.jsonl").read_text().splitlines()]
    assert updated["chars"] == len("# T") and updated["empty"] is False
    assert refresh_outputs(tmp_path, suite, manifest) == set()  # idempotent


# ------------------------------------- model fidelity (olmOCR-Bench failure analysis)
HEADER_ONLY = (
    "```yaml\nprimary_language: en\nis_rotation_valid: True\nrotation_correction: 0\n"
    "is_table: False\nis_diagram: False\n```"
)
VALID = (
    "---\nprimary_language: en\nis_rotation_valid: True\nrotation_correction: 0\n"
    "is_table: False\nis_diagram: False\n---\n# Title\nBody text"
)


def test_olmocr_runs_text_first_and_retries_header_only_output(monkeypatch):
    """9 of 42 olmOCR-Bench pages came back as front matter only (stop, 33 tokens) and
    were accepted; the authors' pipeline retries until the front matter parses."""
    from docingest.adapters.ocr.mlx_vlm import MlxVlmOcr
    from docingest.adapters.ocr.profiles import OLMOCR_LADDER, profile_for
    from docingest.domain.models import ModelRef

    calls, _ = _stub_mlx(monkeypatch, [_gen(HEADER_ONLY, 33, "stop"), _gen(VALID, 400, "stop")])
    templated = []

    class Processor:
        def apply_chat_template(self, messages, **kw):
            templated.append(messages)
            return "text-first prompt"

    engine = MlxVlmOcr(ModelRef(repo_id="org/olmocr", revision="0" * 40), profile_for("olmocr"))
    engine._model, engine._processor, engine._config = object(), Processor(), {}
    res = engine.transcribe(Image.new("RGB", (64, 64), "white"))
    assert [p["type"] for p in templated[0][0]["content"]] == ["text", "image"]
    assert res.text == "# Title\nBody text" and res.attempts == 2
    assert [c["temperature"] for c in calls] == [t for t, _ in OLMOCR_LADDER[:2]]
    assert res.raw_text == VALID  # kept for audits


def test_every_profile_retries_empty_output_and_keeps_the_best_text(monkeypatch):
    from docingest.adapters.ocr.mlx_vlm import MlxVlmOcr
    from docingest.adapters.ocr.profiles import profile_for
    from docingest.domain.models import ModelRef

    _stub_mlx(monkeypatch, [_gen("", 1, "stop"), _gen("Real page", 40, "stop")])
    engine = MlxVlmOcr(ModelRef(repo_id="org/m", revision="0" * 40), profile_for("markdown"))
    engine._model = object()
    res = engine.transcribe(Image.new("RGB", (32, 32), "white"))
    assert res.text == "Real page" and res.attempts == 2


def test_openai_adapter_sends_the_profiles_prompt_order():
    from docingest.adapters.ocr.openai_compat import OpenAICompatibleOcr
    from docingest.adapters.ocr.profiles import profile_for
    from docingest.domain.models import ModelRef

    ref = ModelRef(repo_id="org/m", revision="0" * 40)
    olm = OpenAICompatibleOcr(ref, profile_for("olmocr"), base_url="http://localhost:1")
    gen = OpenAICompatibleOcr(ref, profile_for("markdown"), base_url="http://localhost:1")
    assert [c["type"] for c in olm._content("data:")] == ["text", "image_url"]
    assert [c["type"] for c in gen._content("data:")] == ["image_url", "text"]
    assert (
        olm.fingerprint
        != OpenAICompatibleOcr(
            ref,
            profile_for("markdown", prompt_override=profile_for("olmocr").prompt),
            base_url="http://localhost:1",
        ).fingerprint
    )  # prompt order and ladder are part of the cache key


class RawOcr(FakeOcr):
    """Page "b" needed clean-up (a code fence); the others came back clean."""

    def transcribe(self, image):
        sid = image.info.get("id", "")
        raw = f"```markdown\n{sid}\n```" if sid == "b" else sid
        return OcrResult(sid, 1.0, 10, "stop", raw_text=raw)


def test_runner_keeps_the_raw_model_output_when_cleanup_changed_it(tmp_path):
    from docingest.application.benchmark import RAW_DIR

    runner({"r": RawOcr()}).run(TaggedSuite(), [spec("r")], tmp_path)
    raw_dir = tmp_path / RAW_DIR / "mem" / "r"
    assert sorted(p.name for p in raw_dir.iterdir()) == ["b.txt"]
    assert (raw_dir / "b.txt").read_text().startswith("```markdown")


def test_refresh_outputs_heals_telemetry_that_drifted_from_the_files(tmp_path):
    import json as _json

    from builders import LONG, text_pdf

    from docingest.adapters.datasets.synthetic import SyntheticSuite
    from docingest.entrypoints.bench_cli import refresh_outputs

    suite = SyntheticSuite(
        [(text_pdf(tmp_path / "d.pdf", LONG), [0])], levels=["clean"], min_ref_chars=10
    )
    (sample,) = suite.samples()
    manifest = {
        "suites": {"synthetic": {"candidates": ["c"]}},
        "candidates": {"c": {"profile": "olmocr"}},
    }
    out = suite.output_path(tmp_path, "c", sample)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("")  # already cleaned to nothing by an older refresh...
    tel = tmp_path / "telemetry" / "synthetic"
    tel.mkdir(parents=True)
    rec = {"sample_id": sample.id, "chars": 120, "empty": False}  # ...whose telemetry went stale
    (tel / "c.jsonl").write_text(_json.dumps(rec) + "\n")
    assert refresh_outputs(tmp_path, suite, manifest) == {"c"}
    (healed,) = [_json.loads(x) for x in (tel / "c.jsonl").read_text().splitlines()]
    assert healed["chars"] == 0 and healed["empty"] is True
    assert refresh_outputs(tmp_path, suite, manifest) == set()
