"""Port: transcribe a page image to Markdown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL.Image import Image

from ..domain.models import ModelRef


@dataclass(frozen=True)
class OcrResult:
    text: str
    seconds: float
    gen_tokens: int
    finish_reason: str | None
    peak_memory_gb: float | None = None


class OcrEngine(Protocol):
    fingerprint: str  # model + revision + prompt/profile + generation settings
    model: ModelRef
    dpi: int  # rasterization resolution the engine wants for PDF pages

    def transcribe(self, image: Image) -> OcrResult: ...
