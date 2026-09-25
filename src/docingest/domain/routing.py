"""OCR routing policy: pure decision on measured page signals."""

from __future__ import annotations

from pydantic import BaseModel

from .models import PageProbe, PageSignals


class RoutingPolicy(BaseModel):
    """Thresholds from published pipelines (Marker, olmOCR); tuned in config/pipeline.toml."""

    min_chars: int = 50  # fewer embedded chars than this -> treat as scanned
    image_coverage: float = 0.6  # big image + little text -> scanned
    image_coverage_max_chars: int = 400
    max_garbage_ratio: float = 0.10  # broken font encodings -> re-OCR
    min_alpha_ratio: float = 0.5  # olmOCR heuristic: mostly non-letters -> bad text layer


def decide(signals: PageSignals, policy: RoutingPolicy, *, force_ocr: bool = False) -> PageProbe:
    s, p = signals, policy
    reasons: list[str] = []
    if s.n_chars < p.min_chars:
        reasons.append(f"only {s.n_chars} embedded chars (<{p.min_chars})")
    if s.image_coverage >= p.image_coverage and s.n_chars < p.image_coverage_max_chars:
        reasons.append(f"image covers {s.image_coverage:.0%} of page with little text")
    if s.garbage_ratio > p.max_garbage_ratio:
        reasons.append(f"garbled text layer ({s.garbage_ratio:.0%} bad glyphs)")
    if s.n_chars >= p.min_chars and s.alpha_ratio < p.min_alpha_ratio:
        reasons.append(f"low alphabetic ratio ({s.alpha_ratio:.0%})")
    if force_ocr and not reasons:
        reasons.append("forced (--ocr-all)")
    return PageProbe(**s.model_dump(), needs_ocr=bool(reasons), reasons=reasons)
