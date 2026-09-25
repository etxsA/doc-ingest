"""Command line: ``uv run docingest --help``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .pipeline import Pipeline, dump_index, iter_inputs

app = typer.Typer(add_completion=False, help="Normalize any research document to Markdown.")
console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="pipeline.toml")]


@app.command()
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories")],
    config: ConfigOpt = None,
    force: Annotated[bool, typer.Option(help="Ignore cache")] = False,
    ocr_all: Annotated[bool, typer.Option(help="OCR every page, even born-digital")] = False,
    max_pages: Annotated[int | None, typer.Option(help="Only first N pages")] = None,
) -> None:
    """Detect type, route each page (text layer vs VLM OCR), write Markdown + manifest."""
    cfg = load_config(config)
    pipe = Pipeline(cfg, force=force, ocr_all=ocr_all, max_pages=max_pages, log=console.print)
    manifests = []
    for path in iter_inputs(paths):
        if path.suffix == ".json":  # ground-truth sidecars etc.
            continue
        console.rule(f"[bold]{path.name}")
        try:
            manifest, out = pipe.ingest(path)
        except ValueError as e:
            console.print(f"[yellow]skip[/]: {e}")
            continue
        manifests.append(manifest)
        console.print(f"[green]ok[/] -> {out}/document.md")

    if not manifests:
        return
    dump_index(manifests, Path(cfg.output_dir))
    table = Table(title="Ingestion summary")
    for col in ("document", "kind", "pages", "text-layer", "VLM OCR", "seconds", "doc_id"):
        table.add_column(col)
    for m in manifests:
        table.add_row(
            m.source_name,
            m.source_kind.value,
            str(m.n_pages),
            str(m.n_pages - m.ocr_pages),
            str(m.ocr_pages),
            f"{m.total_seconds:.1f}",
            m.doc_id[:12],
        )
    console.print(table)


@app.command("make-scan")
def make_scan_cmd(
    src: Path,
    out: Path,
    pages: Annotated[str, typer.Option(help="0-based, comma separated")] = "0",
    dpi: int = 150,
) -> None:
    """Rasterize + degrade born-digital pages into an image-only 'scanned' PDF."""
    from .simulate import make_scan

    truth = make_scan(src, out, [int(p) for p in pages.split(",")], dpi=dpi)
    console.print(f"scan -> {out}\nground truth -> {truth}")


@app.command("eval-ocr")
def eval_ocr(
    scanned_pdf: Path,
    config: ConfigOpt = None,
    force: Annotated[bool, typer.Option(help="Ignore cache")] = False,
) -> None:
    """Ingest a simulated scan and score OCR against its ground-truth text layer."""
    from .evaluate import score

    truth = json.loads(scanned_pdf.with_suffix(".truth.json").read_text())
    cfg = load_config(config)
    manifest, out = Pipeline(cfg, force=force, log=console.print).ingest(scanned_pdf)
    md = (out / "document.md").read_text()
    chunks = md.split("<!-- page ")[1:]
    pages = [c.split("-->", 1)[1] for c in chunks]

    table = Table(title=f"OCR quality — {cfg.ocr.repo_id}")
    for col in ("page", "method", "CER", "WER", "word F1", "sec/page"):
        table.add_column(col)
    rows = []
    for rec, ref, hyp in zip(manifest.pages, truth["text"], pages, strict=False):
        s = score(ref, hyp)
        rows.append(s)
        table.add_row(
            str(rec.index + 1),
            rec.method.value,
            f"{s['cer']:.3f}",
            f"{s['wer']:.3f}",
            f"{s['word_f1']:.3f}",
            f"{rec.seconds:.1f}",
        )
    console.print(table)
    report = {"model": cfg.ocr.repo_id, "revision": cfg.ocr.revision, "pages": rows}
    (out / "ocr_eval.json").write_text(json.dumps(report, indent=2))
    console.print(f"report -> {out}/ocr_eval.json")


@app.command()
def ask(
    question: str,
    normalized_dir: Annotated[Path, typer.Option()] = Path("data/normalized"),
) -> None:
    """Answer a question with PaperQA2 over the normalized corpus (needs 'qa' extra)."""
    import asyncio

    from .qa import ask_corpus

    console.print(asyncio.run(ask_corpus(question, normalized_dir)))
