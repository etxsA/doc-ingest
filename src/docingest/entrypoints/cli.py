"""Driving adapter: command line (``uv run docingest --help``). Thin: parse, call use cases."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..application.ingest import SIDECAR_SUFFIX, IngestOptions
from ..bootstrap import REGISTRY, Container, available
from ..config import load_config
from ..domain.text import split_pages
from ..ports import StoredDocument

app = typer.Typer(add_completion=False, help="Normalize any research document to Markdown.")
console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="pipeline.toml")]


def _log(msg: str) -> None:
    console.print(msg, markup=False, highlight=False)


def _container(config: Path | None) -> Container:
    return Container(load_config(config), log=_log)


def iter_inputs(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(x for x in p.rglob("*") if x.is_file() and not x.name.startswith(".")))
        else:
            out.append(p)
    # Sidecars (metadata, ground truth) are not documents.
    return [p for p in out if not p.name.endswith((SIDECAR_SUFFIX, ".truth.json"))]


def _summary(stored: list[StoredDocument]) -> None:
    table = Table(title="Ingestion summary")
    for col in ("document", "kind", "pages", "page methods", "seconds", "doc_id"):
        table.add_column(col)
    for d in stored:
        m = d.manifest
        methods = Counter(p.method.value for p in m.pages)
        pages = str(m.n_pages) if m.complete else f"{m.n_pages}/{m.source_pages}"
        table.add_row(
            escape(m.source_name),
            m.source_kind.value,
            pages,
            ", ".join(f"{k}×{v}" for k, v in methods.most_common()),
            f"{m.total_seconds:.1f}",
            m.doc_id[:12],
        )
    console.print(table)


def _failures(failures: list[tuple[str, str]]) -> None:
    table = Table(title="Failed inputs", style="red")
    table.add_column("input")
    table.add_column("error")
    for name, err in failures:
        table.add_row(escape(name), escape(err))
    console.print(table)


@app.command()
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories")],
    config: ConfigOpt = None,
    force: Annotated[bool, typer.Option(help="Ignore cache")] = False,
    ocr_all: Annotated[bool, typer.Option(help="OCR every PDF page, even born-digital")] = False,
    max_pages: Annotated[int | None, typer.Option(min=1, help="Only first N pages")] = None,
) -> None:
    """Detect type, route each page (text layer / VLM OCR / converter), write Markdown + manifest."""
    service = _container(config).ingest
    options = IngestOptions(force=force, ocr_all=ocr_all, max_pages=max_pages)
    stored, failures = [], []
    for path in iter_inputs(paths):
        console.rule(f"[bold]{escape(path.name)}")
        try:
            doc = service.ingest(path, options)
        except Exception as e:  # one bad file must not stop the batch
            failures.append((path.name, f"{type(e).__name__}: {e}"))
            console.print(f"[red]failed[/]: {escape(str(e))}")
            continue
        stored.append(doc)
        console.print(f"[green]ok[/] -> {escape(doc.location)}/document.md")
    if stored:
        _summary(stored)
    if failures:
        _failures(failures)
        raise typer.Exit(1)


@app.command()
def crawl(
    query: Annotated[str, typer.Argument(help='arXiv query, e.g. "cat:cs.CL AND ti:retrieval"')],
    limit: Annotated[int, typer.Option(min=1, help="Max records")] = 5,
    config: ConfigOpt = None,
    no_ingest: Annotated[bool, typer.Option(help="Only download")] = False,
    max_pages: Annotated[int | None, typer.Option(min=1, help="Only first N pages")] = None,
) -> None:
    """Search arXiv, download LaTeX sources (PDF fallback) with metadata, then ingest them."""
    report = _container(config).crawl.run(
        query, limit, ingest=not no_ingest, options=IngestOptions(max_pages=max_pages)
    )
    if report.ingested:
        _summary(report.ingested)
    console.print(f"fetched {len(report.fetched)}, ingested {len(report.ingested)}")
    if report.failures:
        _failures(report.failures)
        raise typer.Exit(1)


@app.command("make-scan")
def make_scan_cmd(
    src: Path,
    out: Path,
    pages: Annotated[str, typer.Option(help="0-based, comma separated")] = "0",
    dpi: int = 150,
) -> None:
    """Rasterize + degrade born-digital pages into an image-only 'scanned' PDF."""
    from ..adapters.datasets.synthetic import make_scan

    truth = make_scan(src, out, [int(p) for p in pages.split(",")], dpi=dpi)
    console.print(f"scan -> {escape(str(out))}\nground truth -> {escape(str(truth))}")


@app.command("eval-ocr")
def eval_ocr(
    scanned_pdf: Path,
    config: ConfigOpt = None,
    force: Annotated[bool, typer.Option(help="Ignore cache")] = False,
) -> None:
    """Ingest a simulated scan and score OCR against its ground-truth text layer."""
    from ..application.metrics import score

    truth = json.loads(scanned_pdf.with_suffix(".truth.json").read_text())
    container = _container(config)
    stored = container.ingest.ingest(scanned_pdf, IngestOptions(force=force))
    manifest = stored.manifest
    pages = split_pages(container.ingest.store.markdown(stored))
    if len(truth["text"]) != manifest.n_pages:
        raise typer.BadParameter(
            f"{len(truth['text'])} ground-truth pages but {manifest.n_pages} ingested pages"
        )

    def fmt(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.3f}"

    table = Table(title=f"OCR quality — {container.cfg.ocr.repo_id}")
    for col in ("page", "method", "CER", "WER", "word F1", "char3 F1", "sec/page"):
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
            fmt(s["char3_f1"]),
            f"{rec.seconds:.1f}",
        )
    console.print(table)
    report = {
        "model": container.cfg.ocr.repo_id,
        "revision": container.cfg.ocr.revision,
        "scan_sha256": manifest.doc_id,
        "pages": rows,
    }
    (Path(stored.location) / "ocr_eval.json").write_text(json.dumps(report, indent=2))
    console.print(f"report -> {escape(stored.location)}/ocr_eval.json")


@app.command()
def ask(question: str, config: ConfigOpt = None) -> None:
    """Answer a question with PaperQA2 over the normalized corpus (needs the 'qa' extra)."""

    def warn(msg: str) -> None:
        console.print(f"[yellow]warning[/]: {escape(msg)}")

    answer = asyncio.run(_container(config).ask.ask(question, warn=warn))
    console.print(answer, markup=False, highlight=False)  # LLM text may contain [brackets]


@app.command()
def adapters(config: ConfigOpt = None) -> None:
    """List the adapters available for each port and which one is selected."""
    cfg = load_config(config)
    table = Table(title="Ports and adapters")
    for col in ("port", "selected", "available"):
        table.add_column(col)
    for port in REGISTRY:
        table.add_row(port, getattr(cfg.adapters, port), ", ".join(sorted(available(port))))
    console.print(table)


@app.command("model-path")
def model_path(
    role: Annotated[str, typer.Argument(help="llm | embedding | ocr")] = "llm",
    config: ConfigOpt = None,
) -> None:
    """Print the local snapshot path of a pinned model (used by scripts/serve_llm.sh)."""
    from ..adapters.models.huggingface import resolve

    cfg = load_config(config)
    if role == "ocr":
        assert cfg.ocr.revision is not None
        print(resolve(cfg.ocr.repo_id, cfg.ocr.revision))
    elif role in ("llm", "embedding"):
        from ..adapters.qa.paperqa import embedding_path, llm_path

        print(llm_path(cfg) if role == "llm" else embedding_path(cfg))
    else:
        raise typer.BadParameter("role must be one of llm, embedding, ocr")
