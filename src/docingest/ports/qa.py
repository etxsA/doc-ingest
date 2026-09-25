"""Port: answer questions over the normalized corpus."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .store import StoredDocument


class QuestionAnswerer(Protocol):
    async def ask(
        self,
        question: str,
        documents: list[tuple[StoredDocument, str]],  # (document, its Markdown)
        warn: Callable[[str], None],
    ) -> str: ...
