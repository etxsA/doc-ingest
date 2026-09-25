"""docingest: normalize any research document into Markdown + provenance manifest."""

from .pipeline import PIPELINE_VERSION as __version__


def main() -> None:
    from .cli import app

    app()
