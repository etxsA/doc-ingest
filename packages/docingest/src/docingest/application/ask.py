"""Use case: answer a question over the normalized corpus."""

from __future__ import annotations

from collections.abc import Callable

from ..ports import DocumentStore, QuestionAnswerer


class AskService:
    def __init__(self, *, store: DocumentStore, qa: QuestionAnswerer):
        self.store = store
        self.qa = qa

    async def ask(self, question: str, warn: Callable[[str], None] = print) -> str:
        documents, warnings = self.store.corpus()
        for w in warnings:
            warn(w)
        if not documents:
            raise RuntimeError("no ingested documents; run `docingest ingest` first")
        return await self.qa.ask(question, [(d, self.store.markdown(d)) for d in documents], warn)
