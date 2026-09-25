"""Fill the results section of docs/benchmark.md from the benchmark run summaries.

    uv run python scripts/bench_docs.py            # screen + deep runs -> docs/benchmark.md

Replaces everything between the RESULTS markers; every number comes from
data/bench/runs/<run>/summary.json (and per-unit scores), so nothing is typed by hand.
Also copies each run's full report.md next to the doc. Comparisons only: no ranking
language beyond what the statistics support, no recommendation.
"""

from __future__ import annotations

import json
import shutil
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "data" / "bench" / "runs"
DOC = REPO / "docs" / "benchmark.md"
START, END = "<!-- RESULTS -->", "<!-- /RESULTS -->"
CATS = [
    ("arxiv_math", "arXiv math"),
    ("old_scans_math", "old scans math"),
    ("table_tests", "tables"),
    ("old_scans", "old scans"),
    ("headers_footers", "headers/footers"),
    ("multi_column", "multi-column"),
    ("long_tiny_text", "tiny text"),
]


def load(run: str) -> dict | None:
    p = RUNS / run / "summary.json"
    return json.loads(p.read_text()) if p.exists() else None


def pct(v, d=1) -> str:
    return "–" if v is None else f"{v * 100:.{d}f}"


def ci(e: dict | None, d=1) -> str:
    if not e or e.get("mean") is None:
        return "–"
    if e.get("low") is None:
        return pct(e["mean"], d)
    return f"{pct(e['mean'], d)} [{pct(e['low'], d)}, {pct(e['high'], d)}]"


def num(v, fmt="{:.1f}") -> str:
    return "–" if v is None else fmt.format(v)


def scored(suite: dict, metric: str) -> list[str]:
    return [
        n
        for n in suite["ranking"]
        if suite["scores"].get(n, {}).get("metrics", {}).get(metric, {}).get("mean") is not None
    ]


def paired(suite: dict, name: str) -> str:
    if name == suite["best"]:
        return "top mean"
    p = suite.get("paired_vs_best", {}).get(name)
    if not p:
        return "–"
    verdict = "lower (p < 0.05)" if p.get("significant") else "not distinguishable"
    return f"{p['diff'] * 100:+.1f} pts, {_p(p['p_value'])}: {verdict}"


def olmocr_table(summary: dict, title: str) -> list[str]:
    s = summary["suites"]["olmocr-bench"]
    out = [
        f"#### {title}",
        "",
        "| model | pass rate % [95% CI] | official ± | vs top mean (paired) | median s/page | "
        "peak GB | empty | truncated (1st try) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for n in scored(s, "pass_rate"):
        sc, tp = s["scores"][n], s["throughput"].get(n, {})
        hw = sc["details"].get("half_width")
        first = tp.get("first_truncation_rate")
        out.append(
            f"| {n} | {ci(sc['metrics']['pass_rate'])} | {num(hw * 100 if hw else None)} | "
            f"{paired(s, n)} | {num(tp.get('median_s'))} | {num(tp.get('peak_memory_gb'))} | "
            f"{pct(tp.get('empty_rate'), 0)}% | "
            f"{('≥' if tp.get('lower_bound') else '') + pct(first, 0)}% |"
        )
    n_pdfs = next(iter(s["scores"].values()))["n_samples"]
    clusters = next(iter(s["scores"].values()))["details"].get("n_clusters")
    out += [
        "",
        f"{n_pdfs} PDFs ({clusters} clusters). *official ±* is the scorer's own interval "
        "(tests resampled independently, narrower); the bracketed CI resamples whole PDFs "
        "within categories. *vs top mean*: paired cluster bootstrap and sign-flip test against "
        "the candidate with the highest mean; p-values are per comparison, not adjusted for "
        "the number of comparisons (Comparisons below gives the Holm-adjusted verdicts).",
        "",
    ]
    return out


def category_table(summary: dict) -> list[str]:
    s = summary["suites"]["olmocr-bench"]
    out = [
        "| model | " + " | ".join(label for _, label in CATS) + " |",
        "|---|" + "---|" * len(CATS),
    ]
    for n in scored(s, "pass_rate"):
        bc = s["scores"][n]["by_category"]
        cells = [pct(bc.get(c, {}).get("pass_rate", {}).get("mean"), 0) for c, _ in CATS]
        out.append(f"| {n} | " + " | ".join(cells) + " |")
    return [*out, ""]


def median_note(summary: dict | None) -> str:
    """Median page CER per candidate: the means are pulled by a few failure pages."""
    syn = (summary or {}).get("suites", {}).get("synthetic")
    path = RUNS / "screen" / "scores" / "synthetic.json"
    if not syn or not path.exists():
        return ""
    units = json.loads(path.read_text())
    medians = {}
    for n in syn["ranking"]:
        cers = [
            u["cer"] for u in units.get(n, {}).get("units", {}).values() if u.get("cer") is not None
        ]
        if cers:
            medians[n] = statistics.median(cers)
    low = [n for n, m in medians.items() if m < 0.01]
    if not low:
        return ""
    rest = sorted((n for n in medians if n not in low), key=medians.get)
    return (
        f"Median page CER is under 1% for {len(low)} of the {len(medians)} candidates "
        f"({min(medians[n] for n in low) * 100:.2f}-{max(medians[n] for n in low) * 100:.2f}%)"
        + ("; " + ", ".join(f"{n}: {medians[n] * 100:.1f}%" for n in rest) if rest else "")
        + ". Means and their spread come from a few failure pages."
    )


def synthetic_table(summary: dict, run: str) -> list[str]:
    s = summary["suites"]["synthetic"]
    units = json.loads((RUNS / run / "scores" / "synthetic.json").read_text())
    out = [
        "| model | CER % [95% CI] | median CER % | word-F1 | clean | light | heavy | "
        "median s/page | peak GB |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for n in scored(s, "cer"):
        sc, tp = s["scores"][n], s["throughput"].get(n, {})
        bc = sc["by_category"]
        cers = [u["cer"] for u in units[n]["units"].values() if u.get("cer") is not None]
        out.append(
            f"| {n} | {ci(sc['metrics']['cer'])} | {pct(statistics.median(cers)) if cers else '–'}"
            f" | {num(sc['metrics']['word_f1']['mean'], '{:.3f}')} | "
            + " | ".join(
                pct(bc.get(lv, {}).get("cer", {}).get("mean")) for lv in ("clean", "light", "heavy")
            )
            + f" | {num(tp.get('median_s'))} | {num(tp.get('peak_memory_gb'))} |"
        )
    sep = next(iter(s["scores"].values()))["details"].get("reported_separately", {})
    out.append("")
    if note := median_note(summary):
        out += [note, ""]
    for cluster, info in sep.items():

        def page_cer(n: str, cluster: str = cluster) -> str:
            sep_n = s["scores"][n]["details"].get("reported_separately", {})
            return (
                f"{n} {pct(sep_n[cluster]['metrics']['cer']['mean'])}%" if cluster in sep_n else ""
            )

        per_model = ", ".join(c for c in map(page_cer, scored(s, "cer")) if c)
        out += [
            f"**Reported separately: `{cluster}`.** {info['reason']} "
            f"Per model on this page: {per_model}.",
            "",
        ]
    return out


# How candidates are named in the technical report (ids stay in docs/benchmark.md).
DISPLAY = {
    "qwen3.5-9b": "Qwen3.5-9B",
    "qwen3.5-4b": "Qwen3.5-4B",
    "qwen3-vl-30b-a3b": "Qwen3-VL-30B-A3B",
    "qwen3-vl-8b": "Qwen3-VL-8B",
    "qwen3-vl-4b": "Qwen3-VL-4B",
    "qwen3-vl-2b": "Qwen3-VL-2B",
    "olmocr-2-7b-v2": "olmOCR-2-7B-v2",
    "olmocr-2-7b": "olmOCR-2-7B-v1",
    "nanonets-ocr2-3b": "Nanonets-OCR2-3B",
    "glm-ocr": "GLM-OCR",
    "paddleocr-vl": "PaddleOCR-VL",
}


def display(text: str) -> str:
    """Replace candidate ids with display names (longest first, whole tokens only)."""
    import re

    for cid in sorted(DISPLAY, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.-]){re.escape(cid)}(?![\w-])", DISPLAY[cid], text)
    return text


# Candidates that share weights and differ only in how the adapter runs them.
VARIANTS = [
    (
        "olmocr-2-7b-v2",
        "olmocr-2-7b",
        "olmOCR-2 run closer to its authors' pipeline (prompt before the image; their "
        "temperature ladder, retried until the output has front matter followed by page text) "
        "versus the first adapter (image first, retried only on the token cap)",
    ),
]
OLMOCR_KEYS = {
    "primary_language",
    "is_rotation_valid",
    "rotation_correction",
    "is_table",
    "is_diagram",
}


def names_list(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _olm(summary: dict | None) -> dict | None:
    return (summary or {}).get("suites", {}).get("olmocr-bench")


def _syn(summary: dict | None) -> dict | None:
    return (summary or {}).get("suites", {}).get("synthetic")


def _mean(suite: dict, name: str, metric: str) -> float | None:
    return suite["scores"].get(name, {}).get("metrics", {}).get(metric, {}).get("mean")


def complete(suite: dict, metric: str) -> list[str]:
    """Scored candidates whose run finished: a partial run (scoring errors) has a mean over
    the pages done so far, which must not be compared as if it were final."""
    return [n for n in scored(suite, metric) if not suite["scores"][n].get("errors")]


def _p(v: float) -> str:
    return f"p = {v:.3f}" if v >= 0.001 else "p < 0.001"


def _holm(pvals: dict[str, float], alpha: float = 0.05) -> set[str]:
    """Names whose null is rejected by Holm's step-down procedure (family-wise alpha)."""
    rejected: set[str] = set()
    for i, (n, pv) in enumerate(sorted(pvals.items(), key=lambda kv: kv[1])):
        if pv > alpha / (len(pvals) - i):
            break
        rejected.add(n)
    return rejected


def _separation(summary: dict | None, suite_name: str, metric: str, label: str) -> str | None:
    s = (summary or {}).get("suites", {}).get(suite_name)
    if not s or not s.get("best"):
        return None
    names, best = complete(s, metric), s["best"]
    pv = s.get("paired_vs_best", {})
    others = [n for n in names if n != best and pv.get(n) and pv[n].get("p_value") is not None]
    if best not in names or not others:
        return None
    if suite_name == "olmocr-bench":
        n_units = s["scores"][best]["n_samples"]
    else:  # pages reported separately are not in the mean
        n_units = s["scores"][best]["metrics"][metric].get("n") or s["scores"][best]["n_samples"]
    direction = "highest" if s["scores"][best].get("higher_is_better", True) else "lowest"
    unit = "%"
    kind = "PDFs" if suite_name == "olmocr-bench" else "page images"
    tied = [n for n in others if not pv[n].get("significant")]
    apart = [n for n in others if pv[n].get("significant")]
    holm = _holm({n: pv[n]["p_value"] for n in others})

    def item(n: str) -> str:
        return f"{n} ({pv[n]['diff'] * 100:+.1f} pts, {_p(pv[n]['p_value'])})"

    text = (
        f"**{label}: paired tests against the {direction} mean.** {best} has the {direction} "
        f"mean ({pct(_mean(s, best, metric))}{unit}, {n_units} {kind}). "
    )
    if tied:
        text += (
            "Not distinguished from it at the 5% level: "
            + names_list([item(n) for n in tied])
            + ". "
        )
    if apart:
        worse = "higher" if direction == "lowest" else "lower"
        text += f"Distinguished ({worse}): " + names_list([item(n) for n in apart]) + ". "
        kept = [n for n in apart if n in holm]
        text += (
            f"These p-values are per comparison; with a Holm correction for the {len(others)} "
            "comparisons, "
            + (
                "the same verdicts hold."
                if set(kept) == set(apart)
                else (
                    names_list(kept)
                    + (" remains" if len(kept) == 1 else " remain")
                    + " distinguished."
                    if kept
                    else "none remains distinguished."
                )
            )
        )
    return text.strip()


def not_in_deep(screen: dict | None, deep: dict | None) -> str:
    """Candidates the selection rule would include but the deep run does not have (e.g. a
    candidate added to the screening after the deep sample was chosen)."""
    a, b = _olm(screen), _olm(deep)
    if not a or not b:
        return ""
    pv = a.get("paired_vs_best", {})
    missing = [
        n
        for n in complete(a, "pass_rate")
        if n not in b["scores"] and n != a["best"] and pv.get(n) and not pv[n].get("significant")
    ]
    if not missing:
        return ""
    return (
        "Not run on the deep sample, although the screening does not separate "
        + ("it" if len(missing) == 1 else "them")
        + " from the top mean: "
        + names_list([f"{n} ({_p(pv[n]['p_value'])})" for n in missing])
        + "; the deep sample was chosen before "
        + ("it was" if len(missing) == 1 else "they were")
        + " run."
    )


def _subset_mean(score, clusters: set[str]) -> float | None:
    """Mean over categories of the category mean, restricted to units in ``clusters`` (the
    scorer's own aggregation, applied to a subset of PDFs)."""
    groups: dict[str, list[float]] = {}
    for u, vals in score.units.items():
        if score.unit_clusters.get(u, u) in clusters and vals.get(score.primary) is not None:
            groups.setdefault(score.unit_groups.get(u, ""), []).append(vals[score.primary])
    if not groups:
        return None
    return statistics.mean(statistics.mean(v) for v in groups.values())


def _screen_to_deep(screen: dict, deep: dict) -> str | None:
    a, b = _olm(screen), _olm(deep)
    if not a or not b:
        return None
    both = [n for n in complete(b, "pass_rate") if n in complete(a, "pass_rate")]
    if not both:
        return None
    from docingest.application.benchmark import load_scores

    sa = load_scores(RUNS / "screen", "olmocr-bench")
    sb = load_scores(RUNS / "deep", "olmocr-bench")
    shared = set(next(iter(sa.values())).unit_clusters.values())
    added = set(next(iter(sb.values())).unit_clusters.values()) - shared
    n_a, n_b = len(shared), len(shared) + len(added)
    rows, redone = [], []
    for n in both:
        same = _subset_mean(sb[n], shared)
        new = _subset_mean(sb[n], added)
        row = f"{n} {pct(_mean(a, n, 'pass_rate'))} → {pct(_mean(b, n, 'pass_rate'))}"
        if same is not None and abs(same - _mean(a, n, "pass_rate")) >= 0.0005:
            row += (
                f" (same {n_a} PDFs, transcribed again: {pct(same)}; "
                f"{len(added)} added: {pct(new)})"
            )
            redone.append(n)
        elif new is not None:
            row += f" ({len(added)} added: {pct(new)})"
        rows.append(row)
    text = (
        f"**Screening ({n_a} PDFs) → deep ({n_b} PDFs), pass rate.** The deep sample contains the "
        f"screening PDFs and transcribes all of them again: " + "; ".join(rows) + "."
    )
    if redone:
        text += (
            f" For {names_list(redone)} the second transcription of the same pages scored "
            "differently, so part of the change is run-to-run variation, not the added PDFs."
        )
    else:
        text += " Every model scored the same on the shared PDFs in both runs."
    return text


def _category_spread(deep: dict) -> str | None:
    s = _olm(deep)
    if not s:
        return None
    names = complete(s, "pass_rate")
    parts, widths = [], []
    for cat, label in CATS:
        vals = {}
        for n in names:
            est = s["scores"][n]["by_category"].get(cat, {}).get("pass_rate", {})
            if est.get("mean") is None:
                continue
            vals[n] = round(est["mean"] * 100)
            if est.get("low") is not None and est.get("high") is not None:
                widths.append((est["high"] - est["low"]) * 100 / 2)
        if len(vals) < 2:
            continue
        hi, lo = max(vals.values()), min(vals.values())
        top = [n for n, v in vals.items() if v == hi]
        bottom = [n for n, v in vals.items() if v == lo]
        parts.append(f"{label} {lo} ({names_list(bottom)}) to {hi} ({names_list(top)})")
    if not parts:
        return None
    text = "**Per category (deep sample), lowest to highest pass rate:** " + "; ".join(parts) + "."
    if widths:
        per_cat = s["scores"][names[0]]["n_samples"] // len(CATS)
        text += (
            f" Each category score rests on about {per_cat} "
            f"PDFs; their 95% CIs have half-widths of ±{min(widths):.0f} to ±{max(widths):.0f} "
            f"points (median ±{statistics.median(widths):.0f})."
        )
    text += (
        " Tables and headers/footers also depend on the prompt: the general models' Markdown "
        "prompt asks them to omit headers and footers, and GLM-OCR and PaddleOCR-VL ran in "
        "whole-page text mode, not their region-level table mode (see the olmOCR-Bench failure "
        "analysis)."
    )
    return text


def _speed_memory(screen: dict) -> str | None:
    s = _olm(screen)
    if not s:
        return None
    tp = s["throughput"]
    names = [n for n in complete(s, "pass_rate") if tp.get(n, {}).get("median_s")]
    if len(names) < 2:
        return None
    by_t = sorted(names, key=lambda n: tp[n]["median_s"])
    text = (
        f"**Speed and memory (screening).** Median time per page runs from "
        f"{tp[by_t[0]]['median_s']:.1f} s ({by_t[0]}) to {tp[by_t[-1]]['median_s']:.1f} s "
        f"({by_t[-1]}), about {tp[by_t[-1]]['median_s'] / tp[by_t[0]]['median_s']:.0f}x apart."
    )
    peaks = {n: tp[n]["peak_memory_gb"] for n in names if tp[n].get("peak_memory_gb")}
    if len(peaks) >= 2:
        by_m = sorted(peaks, key=peaks.get)
        text += (
            f" Peak memory runs from {peaks[by_m[0]]:.1f} GB ({by_m[0]}) to "
            f"{peaks[by_m[-1]]:.1f} GB ({by_m[-1]}); the next highest is "
            f"{peaks[by_m[-2]]:.1f} GB ({by_m[-2]})."
        )
    return text + " The quality-vs-speed chart plots pass rate against speed."


def _suites_disagree(screen: dict) -> str | None:
    s, syn = _olm(screen), _syn(screen)
    if not s or not syn:
        return None
    both = [n for n in complete(s, "pass_rate") if n in complete(syn, "cer")]
    if len(both) < 3:
        return None
    from docingest.application.benchmark import compare, load_scores

    so = load_scores(RUNS / "screen", "olmocr-bench")
    ss = load_scores(RUNS / "screen", "synthetic")
    reversed_pairs, sig_both = 0, 0
    for i, x in enumerate(both):
        for y in both[i + 1 :]:
            d_olm = _mean(s, x, "pass_rate") - _mean(s, y, "pass_rate")
            d_syn = _mean(syn, y, "cer") - _mean(syn, x, "cer")  # lower CER is better
            if d_olm * d_syn >= 0:
                continue
            reversed_pairs += 1
            if compare(so[x], so[y]).significant and compare(ss[x], ss[y]).significant:
                sig_both += 1
    if not reversed_pairs:
        return None
    n_pairs = len(both) * (len(both) - 1) // 2
    return (
        f"**The two suites order the candidates differently.** Of the {n_pairs} pairs, "
        f"{reversed_pairs} are ordered one way by olmOCR-Bench mean pass rate and the other way by "
        f"synthetic mean CER. Both differences are significant (paired tests, p < 0.05 in each "
        f"suite) for {sig_both or 'none'} of them. Synthetic means are driven by a few failure "
        "pages (see the median CER note); olmOCR-Bench also scores tables, math rendering, "
        "reading order and header/footer removal."
    )


def _blank(path: Path) -> bool:
    """An output with no page text: empty after clean-up, or only olmOCR's metadata header
    (e.g. emitted as JSON, which the front-matter clean-up does not recognise)."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and set(data) <= OLMOCR_KEYS


def _variant(screen: dict) -> list[str]:
    out = []
    for new, old, what in VARIANTS:
        parts = []
        for suite, metric, label in (
            ("olmocr-bench", "pass_rate", "olmOCR-Bench pass rate"),
            ("synthetic", "cer", "synthetic CER"),
        ):
            s = (screen or {}).get("suites", {}).get(suite)
            if not s or not {new, old} <= set(complete(s, metric)):
                continue
            from docingest.application.benchmark import compare, load_scores

            scores = load_scores(RUNS / "screen", suite)
            r = compare(scores[new], scores[old])
            ci_text = "" if r.low != r.low else f" [{r.low * 100:+.1f}, {r.high * 100:+.1f}]"
            verdict = "distinguished" if r.significant else "not distinguished"
            parts.append(
                f"{label} {pct(_mean(s, old, metric))} → {pct(_mean(s, new, metric))} "
                f"(paired {r.diff * 100:+.1f} pts{ci_text}, {_p(r.p_value)}: {verdict} at the "
                "5% level)"
            )
            tp = s["throughput"]
            if suite == "olmocr-bench":
                cats = [
                    f"{label_} {pct(bo, 0)} → {pct(bn, 0)}"
                    for cat, label_ in CATS
                    if (
                        bo := s["scores"][old]["by_category"]
                        .get(cat, {})
                        .get("pass_rate", {})
                        .get("mean")
                    )
                    is not None
                    and (
                        bn := s["scores"][new]["by_category"]
                        .get(cat, {})
                        .get("pass_rate", {})
                        .get("mean")
                    )
                    is not None
                ]
                if cats:
                    parts.append("by category " + ", ".join(cats))
                files = {n: sorted((RUNS / "screen" / suite / n).rglob("*.md")) for n in (old, new)}
                if all(files.values()):
                    blank = {n: sum(map(_blank, fs)) for n, fs in files.items()}
                    parts.append(
                        f"outputs with no page text {blank[old]} → {blank[new]} of "
                        f"{len(files[new])} pages"
                    )
            o, n_ = tp.get(old, {}), tp.get(new, {})
            timing = (
                f"{label.split(' ')[0]} time per page: median {num(o.get('median_s'))} → "
                f"{num(n_.get('median_s'))} s, p90 {num(o.get('p90_s'))} → {num(n_.get('p90_s'))} s"
            )
            if n_.get("retried_rate") is not None:
                timing += f" ({pct(n_['retried_rate'], 0)}% of pages retried)"
            parts.append(timing)
        if parts:
            out.append(
                f"**Same weights, different adapter: {what}.** "
                + "; ".join(parts)
                + ". Verdicts follow the sign-flip test; the bootstrap interval shows the size."
            )
    return out


def observations(screen: dict | None, deep: dict | None) -> list[str]:
    """Data-driven comparison statements (Markdown bold), shared with the PDF builder.
    Every number is computed from the run summaries and scores; none of them picks a model."""
    items = [
        _separation(screen, "olmocr-bench", "pass_rate", "olmOCR-Bench, screening"),
        _separation(deep, "olmocr-bench", "pass_rate", "olmOCR-Bench, deep sample"),
        _separation(screen, "synthetic", "cer", "Synthetic CER"),
        screen and deep and _screen_to_deep(screen, deep),
        deep and _category_spread(deep),
        screen and _speed_memory(screen),
        screen and _suites_disagree(screen),
        *(_variant(screen) if screen else []),
    ]
    return [i for i in items if i]


def results() -> str:
    screen, deep = load("screen"), load("deep")
    out = [START, "", "## Results", ""]
    if screen:
        ver = screen["manifest"]["versions"]
        out += [
            f"Machine: {screen['manifest']['machine']['cpu']}, "
            f"{screen['manifest']['machine']['ram_gb']:.0f} GB; mlx-vlm {ver.get('mlx-vlm')}, "
            f"mlx {ver.get('mlx')}. Full generated reports: "
            "[screening](benchmark/screen_report.md), [deep](benchmark/deep_report.md). "
            "Failure analyses: [synthetic](benchmark/synthetic_failure_analysis.md), "
            "[olmOCR-Bench](benchmark/olmocr_bench_failure_analysis.md).",
            "",
            "![Quality vs speed](benchmark/quality_vs_speed.png)",
            "",
            "### olmOCR-Bench (real scans, math, tables, layout)",
            "",
            *olmocr_table(screen, "Screening: all candidates, 6 PDFs per category"),
        ]
    if deep and "olmocr-bench" in deep["suites"]:
        out += olmocr_table(
            deep,
            "Deep sample, 12 PDFs per category: the candidates the screening did not "
            "separate from the top mean (paired p ≥ 0.05), plus the one with the lowest "
            "synthetic CER",
        )
        if note := not_in_deep(screen, deep):
            out += [note, ""]
    if screen:
        out += [
            "#### Pass rate by category (screening, %)",
            "",
            "![olmOCR-Bench categories](benchmark/olmocr_categories.png)",
            "",
            *category_table(screen),
            "### Synthetic degraded scans (exact ground truth)",
            "",
            *synthetic_table(screen, "screen"),
            "![Synthetic CER by degradation](benchmark/synthetic_cer.png)",
            "",
        ]
    if obs := observations(screen, deep):
        out += [
            "### Comparisons",
            "",
            "Generated from the run summaries and per-unit scores. They describe how the "
            "candidates differ; they do not pick a model.",
            "",
            *(f"- {o}" for o in obs),
            "",
        ]
    out.append(END)
    return "\n".join(out)


def main() -> None:
    doc = DOC.read_text()
    if START not in doc:
        raise SystemExit(f"{DOC}: no {START} marker")
    head, rest = doc.split(START, 1)
    tail = rest.split(END, 1)[1] if END in rest else rest
    DOC.write_text(head + results() + tail)
    dest = REPO / "docs" / "benchmark"
    dest.mkdir(exist_ok=True)
    for run in ("screen", "deep"):
        report = RUNS / run / "report.md"
        if report.exists():
            shutil.copyfile(report, dest / f"{run}_report.md")
    print(DOC)


if __name__ == "__main__":
    main()
