"""Use case: answer a question over the normalized corpus.

Without an index the question goes to the ``QuestionAnswerer`` with the whole corpus, which
retrieves its own evidence. With a ``Retrieval`` configured the evidence is retrieved here in
two stages and handed over as ``contexts``:

1. embed the question once, search the chunk index (dense, plus BM25 for English questions, as
   the index adapter is set up) for the top ``candidates``;
2. rerank those with the cross-encoder, a stable sort by score, and keep the first ``contexts``.
   With no reranker the first ``contexts`` of the first stage are kept as they are.

The answerer then summarizes exactly those chunks and writes the cited answer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain.chunking import Chunk
from ..domain.errors import IndexMismatchError, IndexNotReadyError, RetrievalError
from ..ports import (
    ChunkIndex,
    DocumentStore,
    Embedder,
    KeywordMode,
    QuestionAnswerer,
    Reranker,
    StoredDocument,
)

BUILD_HINT = "run `docingest index build`"


@dataclass(frozen=True)
class Retrieval:
    """The two-stage retrieval in front of the answerer."""

    embedder: Embedder
    index: ChunkIndex
    reranker: Reranker | None  # None: the first stage's order is final
    # Hits the first stage returns. Hits of papers that left the corpus are dropped afterwards,
    # so on an index that was not pruned the reranker may see fewer.
    candidates: int
    contexts: int  # chunks handed to the answerer

    def __post_init__(self) -> None:
        if self.candidates < 1 or self.contexts < 1:
            raise ValueError("candidates and contexts must be at least 1")


class AskService:
    def __init__(
        self,
        *,
        store: DocumentStore,
        qa: QuestionAnswerer,
        retrieval: Callable[[], Retrieval] | None = None,
    ):
        """``retrieval`` is called only for a question that uses the index, so a configuration
        problem with it does not stop one that does not (``use_index=False``)."""
        self.store = store
        self.qa = qa
        self._retrieval = retrieval

    @property
    def has_index(self) -> bool:
        """Whether an index is configured: the default path of ``ask`` then uses it."""
        return self._retrieval is not None

    async def ask(
        self,
        question: str,
        warn: Callable[[str], None] = print,
        *,
        use_index: bool = True,
        keywords: KeywordMode | None = None,
    ) -> str:
        """``use_index=False`` answers with the answerer's own retrieval even when an index is
        configured. ``keywords`` overrides the index's keyword setting for this question."""
        documents, warnings = self.store.corpus()
        for w in warnings:
            warn(w)
        if not documents:
            raise RuntimeError("no ingested documents; run `docingest ingest` first")
        if self._retrieval is None or not use_index:
            if keywords is not None:
                raise ValueError("keywords applies to the index, which this question does not use")
            return await self.qa.ask(
                question, [(d, self.store.markdown(d)) for d in documents], warn
            )
        retrieval = self._retrieval()
        try:
            contexts, sources = retrieve(retrieval, question, documents, warn, keywords)
        finally:
            retrieval.index.close()
        # The answerer needs only the manifests of the papers the chunks come from.
        return await self.qa.ask(question, [(d, "") for d in sources], warn, contexts=contexts)


def retrieve(
    retrieval: Retrieval,
    question: str,
    documents: list[StoredDocument],
    warn: Callable[[str], None],
    keywords: KeywordMode | None = None,
) -> tuple[list[Chunk], list[StoredDocument]]:
    """The chunks to answer from, best first, and the documents they come from (in order of
    first appearance). Raises ``IndexMismatchError`` or ``IndexNotReadyError`` when the index
    cannot answer for this corpus and embedder."""
    by_id = {d.manifest.doc_id: d for d in documents}
    _check_ready(retrieval, by_id, warn)
    vector = retrieval.embedder.embed_query(question)
    hits = retrieval.index.search(question, vector, retrieval.candidates, keywords=keywords)
    chunks = [h.chunk for h in hits if h.chunk.doc_id in by_id]
    if len(chunks) < len(hits):
        warn(
            f"{len(hits) - len(chunks)} of {len(hits)} retrieved chunks belong to documents that "
            f"are no longer in the corpus and were left out ({BUILD_HINT} to prune them)"
        )
    if not hits:  # a rebuild would not change this: the indexed papers have no chunks
        raise RetrievalError("the index found no passage for this question")
    if not chunks:
        raise IndexNotReadyError(
            f"every retrieved chunk is from a document that left the corpus; {BUILD_HINT}"
        )
    if retrieval.reranker is not None:
        scores = retrieval.reranker.rerank(question, chunks)
        if len(scores) != len(chunks):
            raise RetrievalError(
                f"the reranker returned {len(scores)} scores for {len(chunks)} chunks"
            )
        # stable by score: ties keep the first-stage order, as in the measured run
        order = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))
        chunks = [chunks[i] for i in order]
    top = chunks[: retrieval.contexts]
    seen: dict[str, StoredDocument] = {}
    for chunk in top:
        seen.setdefault(chunk.doc_id, by_id[chunk.doc_id])
    return top, list(seen.values())


def _check_ready(
    retrieval: Retrieval, corpus: dict[str, StoredDocument], warn: Callable[[str], None]
) -> None:
    index = retrieval.index
    held, configured = index.embedder_fingerprint, retrieval.embedder.fingerprint
    if held != configured:
        raise IndexMismatchError(
            f"the index holds vectors of embedder {held!r} but the configured one is "
            f"{configured!r}; restore the [embedder] settings or use another [index] dir, "
            f"then {BUILD_HINT}"
        )
    # Before every question, so it reads what the last commit left for search, not every
    # stored document (``stats()`` and ``keys()``, which ``index status`` and ``build`` use).
    state = index.committed()
    if not state.documents:
        stats = index.stats()  # only to word the error; no answer follows, so a full scan is fine
        if stats.documents == 0:
            raise IndexNotReadyError(f"the index is empty; {BUILD_HINT}")
        raise IndexNotReadyError(
            f"the index holds {stats.documents} documents but none is committed, so none can "
            f"be searched (an earlier build was interrupted); {BUILD_HINT}"
        )
    searchable = state.documents
    if state.pending:
        # Rare, so the full scan is fine. A document that is stored but not committed is only
        # pending, not missing. ``stats()`` has the last word on pending: it also sets aside a
        # shard whose header cannot be read, which the file names alone cannot tell.
        searchable = index.keys().keys()
        if index.stats().pending:
            warn(
                f"the index has changes that are not committed yet and are not searched; "
                f"{BUILD_HINT}"
            )
    missing = len(corpus.keys() - searchable)
    if missing:
        warn(
            f"{missing} of {len(corpus)} documents of the corpus are not in the index and cannot "
            f"be used for this answer; {BUILD_HINT}"
        )
