"""Driving adapter: command line (``uv run docingest --help``). Thin: parse, call use cases."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..application.ingest import SIDECAR_SUFFIX, IngestOptions
from ..bootstrap import REGISTRY, Container, available, factory, plugins
from ..config import load_config
from ..domain.errors import DocingestError
from ..domain.text import split_pages
from ..ports import StoredDocument
from .bench_cli import bench_app
from .index_cli import index_app

app = typer.Typer(add_completion=False, help="Normalize any research document to Markdown.")
app.add_typer(bench_app, name="bench")
app.add_typer(index_app, name="index")
console = Console()

# exists=True: a mistyped --config is an error, never a silent switch to the defaults.
ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="pipeline.toml", exists=True, dir_okay=False),
]


def _log(msg: str) -> None:
    console.print(msg, markup=False, highlight=False)


def _container(config: Path | None) -> Container:
    return Container(load_config(config), log=_log)


def iter_inputs(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(
                sorted(x for x in p.rglob("*") if x.is_file() and not x.name.startswith("."))
            )
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


IndexOpt = Annotated[
    bool,
    typer.Option(
        "--index",
        help="Afterwards add the new and changed papers to the chunk index "
        "(needs the index and embedder adapters)",
    ),
]


def _index_ready(container: Container) -> None:
    """Fail before any work is done when ``--index`` cannot work."""
    if not (container.configured("index") and container.configured("embedder")):
        raise typer.BadParameter(
            "--index needs [adapters] index and embedder to be set (see `docingest index --help`)"
        )
    try:
        container.index_service.check_embedder()
    except DocingestError as e:
        console.print(f"[red]error[/]: {escape(str(e))}")
        raise typer.Exit(1) from e


def _index_new(container: Container, stored: list[StoredDocument]) -> None:
    """Add what an ingest stored to the index. The papers are stored whatever happens here."""
    if not stored:
        return
    try:
        report = container.index_service.update(stored)
    except (DocingestError, RuntimeError, ValueError) as e:
        console.print(f"[red]error[/]: the index was not updated: {escape(str(e))}")
        console.print("the papers are stored; run `docingest index build` to catch up")
        raise typer.Exit(1) from e
    console.print(
        f"index: added {report.added}, updated {report.updated}, unchanged {report.unchanged}; "
        f"embedded {report.chunks_embedded} chunks in {report.seconds:.1f}s"
    )


@app.command()
def ingest(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories")],
    config: ConfigOpt = None,
    force: Annotated[bool, typer.Option(help="Ignore cache")] = False,
    ocr_all: Annotated[bool, typer.Option(help="OCR every PDF page, even born-digital")] = False,
    max_pages: Annotated[int | None, typer.Option(min=1, help="Only first N pages")] = None,
    *,
    index: IndexOpt = False,
) -> None:
    """Detect type, route each page (text layer / OCR / converter); write Markdown + manifest."""
    container = _container(config)
    if index:
        _index_ready(container)
    service = container.ingest
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
    if index:
        _index_new(container, stored)
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
    *,
    index: IndexOpt = False,
) -> None:
    """Search arXiv, download LaTeX sources (PDF fallback) with metadata, then ingest them."""
    container = _container(config)
    if index:
        if no_ingest:
            raise typer.BadParameter("--index has nothing to add with --no-ingest")
        _index_ready(container)
    report = container.crawl.run(
        query, limit, ingest=not no_ingest, options=IngestOptions(max_pages=max_pages)
    )
    if report.ingested:
        _summary(report.ingested)
    if index:
        _index_new(container, report.ingested)
    console.print(f"fetched {len(report.fetched)}, ingested {len(report.ingested)}")
    if report.stopped:
        console.print(f"[yellow]crawl stopped early[/]: {escape(report.stopped)}")
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


class KeywordChoice(StrEnum):
    english = "english"
    always = "always"
    never = "never"


@app.command()
def ask(
    question: str,
    config: ConfigOpt = None,
    no_index: Annotated[
        bool,
        typer.Option(
            "--no-index", help="Use PaperQA2's own retrieval even when a chunk index is configured"
        ),
    ] = False,
    bm25: Annotated[
        KeywordChoice | None,
        typer.Option(
            help="When the index adds keyword (BM25) matching: for English questions, always "
            "or never. Overrides [index] bm25 for this question."
        ),
    ] = None,
) -> None:
    """Answer a question with PaperQA2 over the normalized corpus (needs the 'qa' extra).

    With an index configured ([adapters] index, built by `docingest index build`), the
    evidence is retrieved from it and reranked; without one PaperQA2 retrieves it itself.
    """

    def warn(msg: str) -> None:
        console.print(f"[yellow]warning[/]: {escape(msg)}")

    service = _container(config).ask
    using_index = service.has_index and not no_index
    if bm25 is not None and not using_index:
        raise typer.BadParameter("--bm25 needs a configured index, and not --no-index")
    try:
        answer = asyncio.run(
            service.ask(
                question, warn=warn, use_index=not no_index, keywords=bm25.value if bm25 else None
            )
        )
    except DocingestError as e:
        # Expected failures of the retrieval are messages; the answering step, like the plain
        # path, keeps its tracebacks.
        if not using_index:
            raise
        console.print(f"[red]error[/]: {escape(str(e))}")
        raise typer.Exit(1) from e
    console.print(answer, markup=False, highlight=False)  # LLM text may contain [brackets]


def _selected_status(port: str, name: str) -> str:
    """The selected adapter's name, flagged if it is unknown or a plugin that fails to load.

    Only the selected plugin is imported (as a run would): listing never imports one.
    """
    if name in REGISTRY[port]:
        return name
    if name not in plugins(port):
        return f"{name} (unknown)"
    try:
        factory(port, name)
    except Exception as e:  # show a broken plugin instead of crashing the listing
        return f"{name} (broken: {type(e).__name__}: {e})"
    return f"{name} (plugin)"


@app.command()
def adapters(config: ConfigOpt = None) -> None:
    """List the adapters available for each port and which one is selected."""
    cfg = load_config(config)
    table = Table(title="Ports and adapters")
    for col in ("port", "selected", "available"):
        table.add_column(col)
    for port in REGISTRY:
        found = plugins(port)
        names = [
            f"{name} (plugin{', shadowed by the built-in' if name in REGISTRY[port] else ''})"
            if name in found
            else name
            for name in available(port)
        ]
        selected = _selected_status(port, getattr(cfg.adapters, port))
        table.add_row(port, escape(selected), escape(", ".join(names)))
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
