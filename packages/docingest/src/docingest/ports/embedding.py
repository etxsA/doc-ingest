"""Port: turn text into vectors for the chunk index."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

Vector = list[float]


@runtime_checkable
class Embedder(Protocol):
    # What changes the vectors of documents: model, revision, dimensions, dtype, document-side
    # settings. Document vectors with another fingerprint are not comparable. The query
    # instruction is not part of it: it changes only query vectors, so it is exposed apart.
    fingerprint: str
    query_instruction: str  # "" when the model takes none

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        """One vector per text, in order, without any query instruction.

        A text's vector does not depend on the other texts in the call, up to the numerical
        noise of batched inference."""
        ...

    def embed_query(self, text: str) -> Vector:
        """The vector of a question, with the query instruction if the model uses one."""
        ...
