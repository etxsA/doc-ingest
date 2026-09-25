"""Port: transcribe a page image to Markdown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from PIL.Image import Image

from ..domain.models import ModelRef


@dataclass(frozen=True)
class OcrResult:
    """One page's transcription. ``gen_tokens`` / ``finish_reason`` describe the attempt
    whose text this is; engines that retry (a max_tokens hit usually means a repetition
    loop) also report the whole ladder, or benchmarks would hide truncations and
    understate throughput. Single-attempt engines can leave the ladder fields unset.
    """

    text: str
    seconds: float  # wall time of every attempt, prefill included
    gen_tokens: int
    finish_reason: str | None
    peak_memory_gb: float | None = None
    attempts: int = 1
    first_finish_reason: str | None = None  # the first attempt's; defaults to finish_reason
    total_gen_tokens: int | None = None  # over every attempt; defaults to gen_tokens
    gen_seconds: float | None = None  # decode time of every attempt (None: not measured)
    raw_text: str | None = None  # the model's output before clean-up, for audits

    def __post_init__(self) -> None:
        if self.attempts == 1:  # the only attempt is also the first one
            if self.first_finish_reason is None:
                object.__setattr__(self, "first_finish_reason", self.finish_reason)
            if self.total_gen_tokens is None:
                object.__setattr__(self, "total_gen_tokens", self.gen_tokens)


@runtime_checkable
class OcrEngine(Protocol):
    fingerprint: str  # model + revision + prompt/profile + generation settings
    model: ModelRef
    dpi: int  # rasterization resolution the engine wants for PDF pages

    def transcribe(self, image: Image) -> OcrResult: ...
