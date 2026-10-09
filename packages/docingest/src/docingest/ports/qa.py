"""Port: answer questions over the normalized corpus."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..domain.chunking import Chunk
from .store import StoredDocument


@runtime_checkable
class QuestionAnswerer(Protocol):
    async def ask(
        self,
        question: str,
        documents: list[tuple[StoredDocument, str]],  # (document, its Markdown)
        warn: Callable[[str], None],
        contexts: list[Chunk] | None = None,
    ) -> str:
        """Answer from ``documents``, retrieving the evidence itself.

        With ``contexts`` (chunks already retrieved, best first) the adapter answers from
        exactly those chunks and retrieves nothing. It then needs from ``documents`` only
        the manifests of the papers the chunks come from (for the citations): the
        Markdown may be empty. An empty ``contexts`` list is a ``ValueError``: it would read as
        an empty corpus, so the caller decides what no retrieved evidence means.
        """
        ...
