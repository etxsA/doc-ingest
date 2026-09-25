"""Port: load the frames of an image file (multi-page TIFF -> several frames)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PIL.Image import Image


class ImageSource(Protocol):
    def frames(self, path: Path) -> list[Image]:
        """Raise ``DocumentOpenError`` for unreadable images."""
        ...
