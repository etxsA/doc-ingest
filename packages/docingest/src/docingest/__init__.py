"""docingest: normalize any research document into Markdown + provenance manifest.

Hexagonal layout: ``domain`` (pure) <- ``ports`` (Protocols) <- ``application``
(use cases) <- ``adapters`` (implementations) <- ``bootstrap`` (composition root)
<- ``entrypoints`` (CLI, PaperQA hook). See docs/architecture.md.
"""

from .application.ingest import PIPELINE_VERSION as __version__

__all__ = ["__version__"]
