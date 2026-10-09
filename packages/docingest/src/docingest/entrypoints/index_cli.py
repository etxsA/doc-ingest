"""Driving adapter: ``docingest index ...``, the persistent chunk index.

Mounted by the main CLI as ``app.add_typer(index_app, name="index")``. Needs an index and
an embedder selected in ``[adapters]`` (the ``docingest-index`` package provides them).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..application.index import BuildReport, IndexStatus
from ..bootstrap import Container
from ..config import load_config
from ..domain.errors import DocingestError

index_app = typer.Typer(add_completion=False, help="Build and inspect the persistent chunk index.")
console = Console()

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="pipeline.toml", exists=True, dir_okay=False),
]


def _log(msg: str) -> None:
    console.print(msg, markup=False, highlight=False)


def _run[T](config: Path | None, action: Callable[[Container], T]) -> T:
    """Run ``action`` on a container; an expected failure is a message and exit code 1."""
    try:
        return action(Container(load_config(config), log=_log))
    except (DocingestError, RuntimeError, ValueError) as e:
        console.print(f"[red]error[/]: {escape(str(e))}")
        raise typer.Exit(1) from e


def _build_summary(report: BuildReport) -> None:
    console.print(
        f"added {report.added}, updated {report.updated}, unchanged {report.unchanged}, "
        f"pruned {len(report.pruned)}; embedded {report.chunks_embedded} chunks "
        f"in {report.seconds:.1f}s" + ("" if report.committed else "; nothing to commit")
    )
    for w in report.warnings:
        console.print(f"[yellow]warning[/]: {escape(w)}")


def _status_table(status: IndexStatus) -> Table:
    stats = status.stats
    table = Table(title="Chunk index", show_header=False)
    table.add_column("", style="bold")
    table.add_column("")
    rows = [
        ("index", status.index_fingerprint),
        ("embedder", status.embedder_fingerprint),
        ("embedder configured", "same" if status.embedder_matches else "DIFFERENT: build refuses"),
        ("indexed", f"{stats.documents} documents, {stats.chunks} chunks"),
        (
            "searchable",
            f"{stats.searchable_documents} documents, {stats.searchable_chunks} chunks"
            f" (committed {stats.committed_at or 'never'})",
        ),
        ("corpus", f"{status.corpus_documents} documents"),
        ("up to date", str(status.unchanged)),
        ("to add", str(status.new)),
        ("to update", str(status.changed)),
        ("to prune", f"{len(status.stale)} (indexed, no longer in the corpus)"),
        ("to embed", f"{status.chunks_to_embed} chunks"),
    ]
    for key, value in rows:
        table.add_row(key, escape(value))
    return table


@index_app.command()
def build(
    config: ConfigOpt = None,
    no_prune: Annotated[
        bool, typer.Option(help="Keep documents that are no longer in the corpus")
    ] = False,
) -> None:
    """Embed the new and changed documents of the corpus; unchanged ones are skipped."""
    report = _run(config, lambda c: c.index_service.build(prune=not no_prune))
    _build_summary(report)


@index_app.command()
def status(config: ConfigOpt = None) -> None:
    """What is indexed, what is searchable and what a build would do."""
    result = _run(config, lambda c: c.index_service.status())
    console.print(_status_table(result))
    for w in result.warnings:
        console.print(f"[yellow]warning[/]: {escape(w)}")


@index_app.command()
def remove(
    doc_id: Annotated[
        str, typer.Argument(help="doc_id, or a unique prefix of at least 8 characters")
    ],
    config: ConfigOpt = None,
) -> None:
    """Forget one document. The next build adds it again if it is still in the corpus."""
    full = _run(config, lambda c: c.index_service.remove(doc_id))
    console.print(f"removed {full}")
