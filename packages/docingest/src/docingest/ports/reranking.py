"""Port: score candidate chunks against a question with a cross-encoder."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..domain.chunking import Chunk


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, question: str, chunks: Sequence[Chunk]) -> list[float]:
        """One relevance score per chunk, in the order given; larger is more relevant."""
        ...
