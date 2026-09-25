"""Port: answer questions over the normalized corpus."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .store import StoredDocument


@runtime_checkable
class QuestionAnswerer(Protocol):
    async def ask(
        self,
        question: str,
        documents: list[tuple[StoredDocument, str]],  # (document, its Markdown)
        warn: Callable[[str], None],
    ) -> str: ...
