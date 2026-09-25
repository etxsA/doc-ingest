"""OcrEngine adapter: vision-language model in-process on Apple Silicon via mlx-vlm."""

from __future__ import annotations

import json
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from PIL import Image, ImageOps

from ...domain.models import ModelRef
from ...ports import OcrResult
from ..models.huggingface import resolve
from .profiles import OcrProfile


def fit_image(img: Image.Image, max_side: int) -> Image.Image:
    """Upright, opaque RGB no larger than ``max_side`` (phone photos, transparent PNGs)."""
    img = ImageOps.exif_transpose(img)
    if img.has_transparency_data:  # a plain convert("RGB") would turn transparency black
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img.convert("RGBA"))
    img = img.convert("RGB")
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize(
            (round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS
        )
    return img


def _mlx_vlm_version() -> str:
    try:
        return version("mlx-vlm")
    except PackageNotFoundError:
        return "missing"


class MlxVlmOcr:
    """Lazily loads the pinned model once; reused for every page in the run."""

    def __init__(
        self,
        model: ModelRef,
        profile: OcrProfile,
        *,
        dpi: int = 150,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        repetition_penalty: float | None = None,
    ):
        self.model = model
        self.profile = profile
        self.dpi = dpi
        # None -> the profile's model-card defaults
        self.max_tokens = max_tokens or profile.max_tokens
        self.temperature = temperature
        self.repetition_penalty = (
            profile.repetition_penalty if repetition_penalty is None else repetition_penalty
        )
        self.fingerprint = "mlx-vlm " + json.dumps(
            {
                "v": _mlx_vlm_version(),
                "model": model.model_dump(),
                "profile": profile.name,
                "prompt": profile.prompt,
                "max_side": profile.max_side,
                "chat_kwargs": profile.chat_kwargs,
                "dpi": dpi,
                "max_tokens": self.max_tokens,
                "temperature": temperature,
                "repetition_penalty": self.repetition_penalty,
            },
            sort_keys=True,
        )
        # mlx-vlm objects; its type hints are narrower than what it accepts (PIL images).
        self._model: Any = None
        self._processor: Any = None
        self._config: Any = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from mlx_vlm import load  # pyright: ignore[reportPrivateImportUsage]
        from mlx_vlm.utils import load_config

        path = resolve(self.model.repo_id, self.model.revision)
        self._model, self._processor = load(path)
        self._config = load_config(path)

    def unload(self) -> None:
        """Free the weights (benchmarks load many models in one process)."""
        self._model = self._processor = self._config = None
        try:
            import gc

            import mlx.core as mx

            gc.collect()
            mx.clear_cache()
        except Exception:
            pass

    def _attempts(self) -> list[tuple[float, float | None]]:
        # olmOCR-style ladder: hitting max_tokens usually means a repetition loop,
        # so retry with a little sampling temperature and a stronger penalty.
        base = self.repetition_penalty
        return [
            (self.temperature, base),
            (0.2, max(base or 1.0, 1.15)),
            (0.5, max(base or 1.0, 1.25)),
        ]

    def transcribe(self, image: Image.Image) -> OcrResult:
        from mlx_vlm import (
            apply_chat_template,  # pyright: ignore[reportPrivateImportUsage]
            generate,  # pyright: ignore[reportPrivateImportUsage]
        )

        self._ensure_loaded()
        img = fit_image(image, self.profile.max_side)
        prompt: Any = apply_chat_template(
            self._processor,
            self._config,
            self.profile.prompt,
            num_images=1,
            **self.profile.chat_kwargs,
        )
        import mlx.core as mx

        mx.reset_peak_memory()  # per-page peak, not the process-wide high-water mark
        t0 = time.perf_counter()
        res = None
        for temperature, penalty in self._attempts():
            kwargs: dict = {"max_tokens": self.max_tokens, "temperature": temperature}
            if penalty:
                kwargs["repetition_penalty"] = penalty
            images: Any = [img]  # mlx-vlm accepts PIL images; its hint says paths only
            res = generate(
                self._model, self._processor, prompt, image=images, verbose=False, **kwargs
            )
            if res.finish_reason != "length":
                break
        assert res is not None
        return OcrResult(
            text=self.profile.postprocess(res.text),
            seconds=time.perf_counter() - t0,
            gen_tokens=res.generation_tokens,
            finish_reason=res.finish_reason,
            peak_memory_gb=getattr(res, "peak_memory", None),
        )
