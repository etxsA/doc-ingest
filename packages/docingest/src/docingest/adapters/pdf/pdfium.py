"""PdfReader adapter backed by pypdfium2 (PDFium)."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL.Image import Image

from ...domain.errors import DocumentOpenError
from ...domain.models import PageSignals
from ...domain.text import alpha_ratio, garbage_ratio


def _page_space_bounds(obj) -> tuple[float, float, float, float]:
    """Bounds of a page object in page space, including enclosing Form XObjects.

    For objects inside a form, pdfium applies the form's /Matrix but not the CTM
    in effect where the form is drawn, which lives on the container object.
    """
    left, bottom, right, top = obj.get_bounds()
    pts = [(left, bottom), (left, top), (right, bottom), (right, top)]
    container = obj.container
    while container is not None:
        m = container.get_matrix()
        pts = [m.on_point(x, y) for x, y in pts]
        container = container.container
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    return min(xs), min(ys), max(xs), max(ys)


class PdfiumPage:
    def __init__(self, page: pdfium.PdfPage):
        self._page = page

    def signals(self) -> tuple[PageSignals, str]:
        text = self._page.get_textpage().get_text_bounded()
        # Visible page box in unrotated user space (same space as object bounds); it need
        # not start at the origin, and /Rotate must not swap its axes.
        box_l, box_b, box_r, box_t = self._page.get_bbox()
        page_area = max((box_r - box_l) * (box_t - box_b), 1.0)
        image_area, n_images = 0.0, 0
        for obj in self._page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
            left, bottom, right, top = _page_space_bounds(obj)
            left, bottom = max(left, box_l), max(bottom, box_b)
            right, top = min(right, box_r), min(top, box_t)
            image_area += max(right - left, 0) * max(top - bottom, 0)
            n_images += 1
        signals = PageSignals(
            n_chars=len("".join(text.split())),
            n_images=n_images,
            image_coverage=round(min(image_area / page_area, 1.0), 3),
            garbage_ratio=round(garbage_ratio(text), 3),
            alpha_ratio=round(alpha_ratio(text), 3),
        )
        return signals, text

    def render(self, dpi: int) -> Image:
        bitmap = self._page.render(scale=dpi / 72)  # pyright: ignore[reportArgumentType]  # pypdfium2 accepts float scale
        return bitmap.to_pil()


class PdfiumDocument:
    def __init__(self, pdf: pdfium.PdfDocument):
        self._pdf = pdf
        self.title = (pdf.get_metadata_dict().get("Title") or "").strip() or None

    def __len__(self) -> int:
        return len(self._pdf)

    def page(self, index: int) -> PdfiumPage:
        return PdfiumPage(self._pdf[index])

    def close(self) -> None:
        self._pdf.close()


class PdfiumReader:
    fingerprint = f"pypdfium2 {version('pypdfium2')}"

    def open(self, path: Path) -> PdfiumDocument:
        try:
            return PdfiumDocument(pdfium.PdfDocument(path))
        except pdfium.PdfiumError as e:
            raise DocumentOpenError(f"cannot open PDF {path.name}: {e}") from e
