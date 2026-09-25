"""ImageSource adapter backed by Pillow."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

from ...domain.errors import DocumentOpenError


class PillowImageSource:
    def frames(self, path: Path) -> list[Image.Image]:
        try:
            with Image.open(path) as img:
                return [f.copy() for f in ImageSequence.Iterator(img)]  # multi-page TIFF
        except (UnidentifiedImageError, OSError) as e:
            raise DocumentOpenError(f"cannot read image {path.name}: {e}") from e
