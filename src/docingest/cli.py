"""Command line: ``uv run docingest --help``."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import load_config
from .pipeline import Pipeline, dump_index, iter_inputs, split_pages

app = typer.Typer(add_completion=False, help="Normalize any research document to Markdown.")
console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="pipeline.toml")]


def _log(msg: str) -> None:
    console.print(msg, markup=False, highlight=False)


@app.command()
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories")],
    config: ConfigOpt = None,
    force: Annotated[bool, typer.Option(help="Ignore cache")] = False,
    ocr_all: Annotated[bool, typer.Option(help="OCR every page, even born-digital")] = False,
    max_pages: Annotated[int | None, typer.Option(min=1, help="Only first N pages")] = None,
) -> None:
    """Detect type, route each page (text layer vs VLM OCR), write Markdown + manifest."""
    cfg = load_config(config)
    pipe = Pipeline(cfg, force=force, ocr_all=ocr_all, max_pages=max_pages, log=_log)
    manifests, failures = [], []
    try:
        for path in iter_inputs(paths):
            if path.suffix == ".json":  # ground-truth sidecars etc.
                continue
            console.rule(f"[bold]{escape(path.name)}")
            try:
                manifest, out = pipe.ingest(path)
            except Exception as e:  # one bad file must not stop the batch
                failures.append((path, f"{type(e).__name__}: {e}"))
                console.print(f"[red]failed[/]: {escape(str(e))}")
                continue
            manifests.append(manifest)
            console.print(f"[green]ok[/] -> {escape(str(out))}/document.md")
    finally:
        if manifests:
            dump_index(manifests, Path(cfg.output_dir))

    if manifests:
        table = Table(title="Ingestion summary")
        for col in ("document", "kind", "pages", "page methods", "seconds", "doc_id"):
            table.add_column(col)
        for m in manifests:
            methods = Counter(p.method.value for p in m.pages)
            pages = str(m.n_pages) if m.n_pages == m.source_pages else f"{m.n_pages}/{m.source_pages}"
            table.add_row(
                escape(m.source_name),
                m.source_kind.value,
                pages,
                ", ".join(f"{k}×{v}" for k, v in methods.most_common()),
                f"{m.total_seconds:.1f}",
                m.doc_id[:12],
            )
        console.print(table)
    if failures:
        table = Table(title="Failed inputs", style="red")
        table.add_column("file")
        table.add_column("error")
        for path, err in failures:
            table.add_row(escape(path.name), escape(err))
        console.print(table)
        raise typer.Exit(1)


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
    console.print(f"scan -> {escape(str(out))}\nground truth -> {escape(str(truth))}")


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
    manifest, out = Pipeline(cfg, force=force, log=_log).ingest(scanned_pdf)
    pages = split_pages((out / "document.md").read_text())
    if len(truth["text"]) != manifest.n_pages:
        raise typer.BadParameter(
            f"{len(truth['text'])} ground-truth pages but {manifest.n_pages} ingested pages"
        )

    def fmt(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.3f}"

    table = Table(title=f"OCR quality — {cfg.ocr.repo_id}")
    for col in ("page", "method", "CER", "WER", "word F1", "sec/page"):
        table.add_column(col)
    rows = []
    for rec, ref in zip(manifest.pages, truth["text"], strict=True):
        s = score(ref, pages[rec.index + 1])
        rows.append(s)
        table.add_row(
            str(rec.index + 1),
            rec.method.value,
            fmt(s["cer"]),
            fmt(s["wer"]),
            fmt(s["word_f1"]),
            f"{rec.seconds:.1f}",
        )
    console.print(table)
    report = {
        "model": cfg.ocr.repo_id,
        "revision": cfg.ocr.revision,
        "scan_sha256": manifest.doc_id,
        "pages": rows,
    }
    (out / "ocr_eval.json").write_text(json.dumps(report, indent=2))
    console.print(f"report -> {escape(str(out))}/ocr_eval.json")


@app.command()
def ask(
    question: str,
    normalized_dir: Annotated[Path, typer.Option()] = Path("data/normalized"),
    config: ConfigOpt = None,
) -> None:
    """Answer a question with PaperQA2 over the normalized corpus (needs 'qa' extra)."""
    import asyncio

    from .qa import ask_corpus

    def warn(msg: str) -> None:
        console.print(f"[yellow]warning[/]: {escape(msg)}")

    answer = asyncio.run(ask_corpus(question, normalized_dir, load_config(config), warn=warn))
    console.print(answer, markup=False, highlight=False)  # LLM text may contain [brackets]


@app.command("model-path")
def model_path(
    role: Annotated[str, typer.Argument(help="llm | embedding | ocr")] = "llm",
    config: ConfigOpt = None,
) -> None:
    """Print the local snapshot path of a pinned model (used by scripts/serve_llm.sh)."""
    from .models import embedding_path, llm_path, resolve

    cfg = load_config(config)
    paths = {
        "llm": lambda: llm_path(cfg),
        "embedding": lambda: embedding_path(cfg),
        "ocr": lambda: resolve(cfg.ocr.repo_id, cfg.ocr.revision),
    }
    if role not in paths:
        raise typer.BadParameter(f"role must be one of {sorted(paths)}")
    print(paths[role]())
