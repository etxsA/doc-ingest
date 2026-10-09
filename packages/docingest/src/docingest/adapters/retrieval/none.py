"""The ``none`` adapters: the default of ``embedder``, ``index`` and ``reranker``.

They mean "no chunk index configured" and keep ``docingest`` working without a package
that provides real ones. Every operation fails with a pointer to the ``[adapters]`` key,
so nothing silently pretends to search.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from ...domain.chunking import Chunk
from ...domain.errors import NotConfiguredError
from ...ports import Hit, IndexStats, Vector


def _not_configured(port: str) -> NoReturn:
    raise NotConfiguredError(f'[adapters] {port} is "none": select an adapter to use it')


class NoEmbedder:
    fingerprint = "none"
    query_instruction = ""

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        _not_configured("embedder")

    def embed_query(self, text: str) -> Vector:
        _not_configured("embedder")


class NoIndex:
    fingerprint = "none"
    embedder_fingerprint = "none"

    def keys(self) -> dict[str, str]:
        _not_configured("index")

    def upsert(
        self, doc_id: str, key: str, chunks: Sequence[Chunk], vectors: Sequence[Vector]
    ) -> None:
        _not_configured("index")

    def remove(self, doc_id: str) -> None:
        _not_configured("index")

    def commit(self) -> None:
        _not_configured("index")

    def stats(self) -> IndexStats:
        _not_configured("index")

    def search(self, question: str, vector: Vector, k: int) -> list[Hit]:
        _not_configured("index")


class NoReranker:
    def rerank(self, question: str, chunks: Sequence[Chunk]) -> list[float]:
        _not_configured("reranker")
