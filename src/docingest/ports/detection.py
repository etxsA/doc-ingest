"""Port: identify what kind of input a file is."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.models import SourceKind


class TypeDetector(Protocol):
    def detect(self, path: Path) -> tuple[SourceKind, str]:
        """Return (kind, mime). Raise ``UnsupportedInputError`` when unknown."""
        ...
