"""DocumentConverter adapter for Markdown / plain text: passthrough, one segment."""

from __future__ import annotations

from pathlib import Path

from ...domain.models import PageMethod
from ...ports import Conversion, Segment


class PassthroughConverter:
    fingerprint = "passthrough 1"

    def convert(self, path: Path) -> Conversion:
        text = path.read_text(errors="replace")
        return Conversion(segments=[Segment(text)], method=PageMethod.PASSTHROUGH, engine="passthrough")
