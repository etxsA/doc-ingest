"""Charts for a benchmark run: quality vs speed, olmOCR-Bench categories, synthetic CER.

    uv run python scripts/bench_charts.py data/bench/runs/screen docs/benchmark

Reads <run>/summary.json (written by `docingest bench report`) and writes PNGs.
Colors follow a validated palette (scripts: dataviz validator): three family hues
(all-pairs CVD-safe) with direct labels on every point, and a one-hue blue ramp for
magnitudes and ordered levels.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
FAMILY = {  # categorical slots 1-3 of the reference palette (validated all-pairs)
    "Qwen3-VL": "#2a78d6",
    "Qwen3.5": "#eb6834",
    "OCR-specialized": "#1baf7a",
}
RAMP = [  # sequential blue 100 -> 700
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
]
LEVELS = {"clean": "#86b6ef", "light": "#2a78d6", "heavy": "#104281"}  # ordinal, validated
CATEGORIES = [
    "arxiv_math",
    "old_scans_math",
    "table_tests",
    "old_scans",
    "headers_footers",
    "multi_column",
    "long_tiny_text",
]
CATEGORY_LABEL = {
    "arxiv_math": "arXiv math",
    "old_scans_math": "old scans\nmath",
    "table_tests": "tables",
    "old_scans": "old scans",
    "headers_footers": "headers/\nfooters",
    "multi_column": "multi-\ncolumn",
    "long_tiny_text": "tiny text",
}


def family(name: str) -> str:
    if name.startswith("qwen3-vl"):
        return "Qwen3-VL"
    if name.startswith("qwen3.5"):
        return "Qwen3.5"
    return "OCR-specialized"


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=9, length=0)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def figure(w: float, h: float):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    return fig, ax


def title(ax, text: str, sub: str) -> None:
    ax.set_title(text, loc="left", fontsize=12, color=TEXT, fontweight="bold", pad=22)
    ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=8.5, color=TEXT_2, va="bottom")


def place_labels(fig, ax, points: list[tuple[str, float, float]]) -> None:
    """Direct labels without collisions: try positions around each point, keep the first
    whose box overlaps no placed label and no marker (greedy, highest points first)."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    to_disp = ax.transData.transform
    markers = [to_disp((x, y)) for _, x, y in points]
    placed = []
    offsets = [
        (7, 3, "left"),
        (7, -11, "left"),
        (-7, 3, "right"),
        (-7, -11, "right"),
        (7, 13, "left"),
        (7, -21, "left"),
        (-7, 13, "right"),
        (-7, -21, "right"),
    ]

    frame = ax.get_window_extent(renderer)

    def clash(bb) -> bool:
        if bb.x0 < frame.x0 or bb.x1 > frame.x1 or bb.y0 < frame.y0 or bb.y1 > frame.y1:
            return True  # keep labels inside the plot (never over the legend)
        pad = bb.expanded(1.05, 1.15)
        if any(pad.overlaps(p) for p in placed):
            return True
        return any(
            pad.x0 - 5 <= mx <= pad.x1 + 5 and pad.y0 - 5 <= my <= pad.y1 + 5 for mx, my in markers
        )

    for name, x, y in sorted(points, key=lambda p: -p[2]):
        for dx, dy, ha in offsets:
            t = ax.annotate(
                name,
                (x, y),
                xytext=(dx, dy),
                textcoords="offset points",
                fontsize=8,
                color=TEXT,
                ha=ha,
            )
            bb = t.get_window_extent(renderer)
            if not clash(bb) or (dx, dy, ha) == offsets[-1]:
                placed.append(bb)
                break
            t.remove()


def quality_vs_speed(summary: dict, out: Path) -> Path | None:
    suite = summary["suites"].get("olmocr-bench")
    if not suite:
        return None
    fig, ax = figure(8.2, 4.8)
    seen = set()
    points: list[tuple[str, float, float]] = []
    for name, sc in suite["scores"].items():
        m = sc["metrics"]["pass_rate"]
        tp = suite["throughput"].get(name, {})
        if m.get("mean") is None or not tp.get("median_s"):
            continue
        x, y = tp["median_s"], m["mean"] * 100
        fam = family(name)
        c = FAMILY[fam]
        if m.get("low") is not None:
            ax.plot([x, x], [m["low"] * 100, m["high"] * 100], color=c, lw=1.2, alpha=0.45)
        ax.scatter(
            [x],
            [y],
            s=64,
            color=c,
            edgecolor=SURFACE,
            linewidth=2,
            zorder=3,
            label=fam if fam not in seen else None,
        )
        seen.add(fam)
        points.append((name, x, y))
    ax.set_xscale("log")
    xs = [x for _, x, _ in points]
    ax.set_xlim(min(xs) / 1.25, max(xs) * 1.9)  # headroom so right-hand labels fit inside
    lo, hi = ax.get_xlim()
    ticks = [t for t in (1, 2, 3, 5, 10, 20, 30, 50, 100) if lo <= t <= hi]
    ax.xaxis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("median seconds per page (log scale, M4 Pro 24 GB)", color=TEXT_2, fontsize=9)
    ax.set_ylabel("olmOCR-Bench pass rate (%)", color=TEXT_2, fontsize=9)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g} s"))
    place_labels(fig, ax, points)
    title(
        ax,
        "Quality vs speed",
        "Up: higher pass rate. Left: faster. Bars are 95% CIs (PDFs resampled within categories).",
    )
    ax.legend(
        frameon=False,
        fontsize=8.5,
        labelcolor=TEXT_2,
        title="family",
        title_fontsize=8.5,
        loc="upper left",
        bbox_to_anchor=(1.01, 1.0),
    )
    fig.tight_layout()
    path = out / "quality_vs_speed.png"
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def categories(summary: dict, out: Path) -> Path | None:
    suite = summary["suites"].get("olmocr-bench")
    if not suite:
        return None
    rows = [
        n
        for n in suite.get("ranking", [])
        if suite["scores"].get(n, {}).get("metrics", {}).get("pass_rate", {}).get("mean")
        is not None
    ]
    cols = [c for c in CATEGORIES if any(c in suite["scores"][r]["by_category"] for r in rows)]
    grid = []
    for r in rows:
        bc = suite["scores"][r]["by_category"]
        grid.append([bc.get(c, {}).get("pass_rate", {}).get("mean") for c in cols])
    fig, ax = figure(1.2 + 0.95 * len(cols), 0.9 + 0.42 * len(rows))
    ax.grid(False)
    cmap = ListedColormap(RAMP)
    values = [[(v if v is not None else float("nan")) for v in row] for row in grid]
    ax.imshow(values, cmap=cmap, vmin=0, vmax=1, aspect="auto")
    for i, row in enumerate(grid):
        for j, v in enumerate(row):
            if v is None:
                continue
            dark = v >= 0.55  # white text on the darker half of the ramp
            ax.text(
                j,
                i,
                f"{v * 100:.0f}",
                ha="center",
                va="center",
                fontsize=8.5,
                color="#ffffff" if dark else TEXT,
            )
    ax.set_xticks(range(len(cols)), [CATEGORY_LABEL[c] for c in cols], fontsize=8)
    ax.set_yticks(range(len(rows)), rows, fontsize=8.5)
    ax.tick_params(colors=TEXT, length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    title(
        ax,
        "olmOCR-Bench pass rate by category (%)",
        "Rows ordered by mean pass rate; each cell is one category's test pass rate.",
    )
    fig.tight_layout()
    path = out / "olmocr_categories.png"
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def synthetic(summary: dict, out: Path) -> Path | None:
    suite = summary["suites"].get("synthetic")
    if not suite:
        return None
    rows = [n for n in suite.get("ranking", []) if n in suite["scores"]]
    first = suite["scores"][rows[0]]
    n_sep = len(first["details"].get("reported_separately", {}))
    n_pages = (first["metrics"]["cer"].get("n") or first["n_samples"]) // len(LEVELS)
    fig, ax = figure(7.2, 1.6 + 0.36 * len(rows))
    for i, name in enumerate(rows):
        bc = suite["scores"][name]["by_category"]
        xs = [
            bc[lv]["cer"]["mean"] * 100
            for lv in LEVELS
            if lv in bc and bc[lv]["cer"]["mean"] is not None
        ]
        if xs:
            ax.plot([min(xs), max(xs)], [i, i], color=GRID, lw=1.5, zorder=1)
        for lv, c in LEVELS.items():
            if lv in bc and bc[lv]["cer"]["mean"] is not None:
                ax.scatter(
                    [bc[lv]["cer"]["mean"] * 100],
                    [i],
                    s=56,
                    color=c,
                    edgecolor=SURFACE,
                    linewidth=2,
                    zorder=3,
                    label=lv if i == 0 else None,
                )
    ax.set_yticks(range(len(rows)), rows, fontsize=8.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)  # in summary order, with breathing room
    ax.set_xlabel("character error rate (%) - lower is better", color=TEXT_2, fontsize=9)
    ax.grid(axis="y", visible=False)
    title(
        ax,
        "Synthetic degraded scans: CER by degradation level",
        f"{n_pages} born-digital pages x 3 levels"
        + (f" (+{n_sep} reported separately)" if n_sep else "")
        + "; reference: text layer minus furniture.",
    )
    ax.legend(
        frameon=False,
        fontsize=8.5,
        labelcolor=TEXT_2,
        title="degradation",
        title_fontsize=8.5,
        loc="upper left",
        bbox_to_anchor=(1.01, 1.0),
    )
    fig.tight_layout()
    path = out / "synthetic_cer.png"
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def main(run_dir: str, out_dir: str) -> None:
    summary = json.loads((Path(run_dir) / "summary.json").read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for fn in (quality_vs_speed, categories, synthetic):
        if path := fn(summary, out):
            print(path)


if __name__ == "__main__":
    main(*sys.argv[1:3])
