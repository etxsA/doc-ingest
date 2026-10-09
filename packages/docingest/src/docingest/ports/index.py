"""Port: a persistent index of document chunks with a first-stage search."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.chunking import Chunk
from .embedding import Vector


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # larger is better; comparable only within one result list


@runtime_checkable
class ChunkIndex(Protocol):
    fingerprint: str  # embedder + chunker settings + format: an index of another one is not reused

    def keys(self) -> dict[str, str]:
        """``{doc_id: key}`` of every indexed document, where ``key`` is what ``upsert`` got.
        It reflects every ``upsert`` and ``remove``, committed or not."""
        ...

    def upsert(
        self, doc_id: str, key: str, chunks: Sequence[Chunk], vectors: Sequence[Vector]
    ) -> None:
        """Replace everything indexed for ``doc_id``; ``ValueError`` unless there is one
        vector per chunk. ``key`` identifies the content the chunks were cut from (callers
        skip a document whose key is unchanged). Not visible to ``search`` before ``commit``."""
        ...

    def remove(self, doc_id: str) -> None:
        """Forget a document; unknown ids are ignored. Not visible to ``search`` before
        ``commit``."""
        ...

    def commit(self) -> None:
        """After this, ``search`` reflects exactly what ``keys()`` reports.

        That covers every ``upsert`` and ``remove`` since the last commit, and also documents
        an earlier process stored but never committed (an adapter that writes at ``upsert``
        time can be interrupted before this call). An adapter that keeps a search layer apart
        from its stored chunks (a dense matrix, a keyword index) rebuilds it here, once per
        batch instead of once per document."""
        ...

    def search(self, question: str, vector: Vector, k: int) -> list[Hit]:
        """At most ``k`` hits among the committed documents, best first. ``vector`` is the
        embedded question; the text is for adapters that also match keywords."""
        ...
