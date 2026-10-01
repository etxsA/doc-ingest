"""Port: load the frames of an image file (multi-page TIFF -> several frames)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL.Image import Image


@runtime_checkable
class ImageSource(Protocol):
    fingerprint: str  # name + version: part of the cache key of image inputs

    def frames(self, path: Path) -> list[Image]:
        """Raise ``DocumentOpenError`` for unreadable images."""
        ...
