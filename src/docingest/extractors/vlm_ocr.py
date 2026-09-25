"""OCR of page images with a local vision-language model (Qwen-VL family) on MLX."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from PIL import Image

from ..config import OcrConfig

_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


@dataclass
class OcrResult:
    text: str
    seconds: float
    gen_tokens: int
    finish_reason: str | None


def fit_image(img: Image.Image, max_side: int) -> Image.Image:
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
        from huggingface_hub import snapshot_download
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        try:  # offline-first: pinned revision already in the local HF cache
            path = snapshot_download(
                self.cfg.repo_id, revision=self.cfg.revision, local_files_only=True
            )
        except Exception:
            path = snapshot_download(self.cfg.repo_id, revision=self.cfg.revision)
        self._model, self._processor = load(path)
        self._config = load_config(path)

    def __call__(self, image: Image.Image) -> OcrResult:
        from mlx_vlm import apply_chat_template, generate

        self._ensure_loaded()
        img = fit_image(image, self.cfg.max_side)
        prompt = apply_chat_template(
            self._processor, self._config, self.cfg.prompt, num_images=1
        )
        kwargs: dict = {"max_tokens": self.cfg.max_tokens, "temperature": self.cfg.temperature}
        if self.cfg.repetition_penalty:
            kwargs["repetition_penalty"] = self.cfg.repetition_penalty
        t0 = time.perf_counter()
        res = generate(self._model, self._processor, prompt, image=[img], verbose=False, **kwargs)
        return OcrResult(
            text=clean_output(res.text),
            seconds=time.perf_counter() - t0,
            gen_tokens=res.generation_tokens,
            finish_reason=res.finish_reason,
        )
