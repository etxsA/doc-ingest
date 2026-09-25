"""OCR of page images with a local vision-language model (Qwen-VL family) on MLX."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from PIL import Image, ImageOps

from ..config import OcrConfig

_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


@dataclass
class OcrResult:
    text: str
    seconds: float
    gen_tokens: int
    finish_reason: str | None


def fit_image(img: Image.Image, max_side: int) -> Image.Image:
    """Upright, opaque RGB no larger than ``max_side`` (phone photos, transparent PNGs)."""
    img = ImageOps.exif_transpose(img)
    if img.has_transparency_data:  # a plain convert("RGB") would turn transparency black
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img.convert("RGBA"))
    img = img.convert("RGB")
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img


def clean_output(text: str) -> str:
    """Strip wrappers VLMs like to add around the transcription."""
    text = text.strip()
    if m := _FENCE.match(text):
        text = m.group(1).strip()
    return text


class VlmOcr:
    """Lazily loads the pinned model once; reused for every page in the run."""

    def __init__(self, cfg: OcrConfig):
        self.cfg = cfg
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        from ..models import resolve

        path = resolve(self.cfg.repo_id, self.cfg.revision)
        self._model, self._processor = load(path)
        self._config = load_config(path)

    def __call__(self, image: Image.Image) -> OcrResult:
        from mlx_vlm import apply_chat_template, generate

        self._ensure_loaded()
        img = fit_image(image, self.cfg.max_side)
        prompt = apply_chat_template(
            self._processor, self._config, self.cfg.prompt, num_images=1
        )
        t0 = time.perf_counter()
        # olmOCR-style ladder: hitting max_tokens usually means a repetition loop,
        # so retry with a little sampling temperature and a stronger penalty.
        for temperature, penalty in self._attempts():
            kwargs: dict = {"max_tokens": self.cfg.max_tokens, "temperature": temperature}
            if penalty:
                kwargs["repetition_penalty"] = penalty
            res = generate(
                self._model, self._processor, prompt, image=[img], verbose=False, **kwargs
            )
            if res.finish_reason != "length":
                break
        return OcrResult(
            text=clean_output(res.text),
            seconds=time.perf_counter() - t0,
            gen_tokens=res.generation_tokens,
            finish_reason=res.finish_reason,
        )

    def _attempts(self) -> list[tuple[float, float | None]]:
        base = self.cfg.repetition_penalty
        return [
            (self.cfg.temperature, base),
            (0.2, max(base or 1.0, 1.15)),
            (0.5, max(base or 1.0, 1.25)),
        ]
