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
    return f"{p['diff'] * 100:+.1f} pts, p = {p['p_value']:.3f}: {verdict}"


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
        "within categories. *vs top mean*: paired cluster bootstrap / sign-flip test against "
        "the candidate with the highest mean.",
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
    for cluster, info in sep.items():
        out += [
            f"**Reported separately: `{cluster}`.** {info['reason']} Per model on this page: "
            + ", ".join(
                f"{n} {pct(s['scores'][n]['details']['reported_separately'][cluster]['metrics']['cer']['mean'])}%"
                for n in scored(s, "cer")
                if cluster in s["scores"][n]["details"].get("reported_separately", {})
            )
            + ".",
            "",
        ]
    return out


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
            "Deep sample: candidates the screening could not separate "
            "from the top mean, 12 PDFs per category",
        )
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
