"""OcrEngine adapter: vision-language model in-process on Apple Silicon via mlx-vlm."""

from __future__ import annotations

import json
import time
import zlib
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from PIL import Image, ImageOps

from ...domain.models import ModelRef
from ...ports import OcrResult
from ..models.huggingface import resolve
from .profiles import OcrProfile, acceptable, attempts


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


def _decode_seconds(res: Any) -> float | None:
    """Decode time of one mlx-vlm generation (its tokens / its tokens per second)."""
    tps = getattr(res, "generation_tps", None)
    tokens = getattr(res, "generation_tokens", None)
    if not tps or tokens is None:
        return 0.0 if tokens == 0 else None
    return tokens / tps


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
                "profile_code": profile.code_version,
                "prompt": profile.prompt,
                "max_side": profile.max_side,
                "chat_kwargs": profile.chat_kwargs,
                "prompt_first": profile.prompt_first,
                "ladder": profile.ladder,
                "validated": profile.valid is not None,
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

    def _prompt(self) -> Any:
        from mlx_vlm import apply_chat_template  # pyright: ignore[reportPrivateImportUsage]

        if self.profile.prompt_first:  # the model was trained text-then-image (olmOCR)
            messages = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": self.profile.prompt}, {"type": "image"}],
                }
            ]
            return self._processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, **self.profile.chat_kwargs
            )
        # mlx-vlm's default order for the model type (image first for the Qwen-VL family)
        return apply_chat_template(
            self._processor,
            self._config,
            self.profile.prompt,
            num_images=1,
            **self.profile.chat_kwargs,
        )

    def transcribe(self, image: Image.Image) -> OcrResult:
        from mlx_vlm import generate  # pyright: ignore[reportPrivateImportUsage]

        self._ensure_loaded()
        img = fit_image(image, self.profile.max_side)
        prompt = self._prompt()
        import mlx.core as mx

        mx.reset_peak_memory()  # per-page peak, not the process-wide high-water mark
        # Sampled attempts are seeded from the page itself: rerunning the benchmark
        # reproduces a retried page's text instead of drawing a new one.
        seed = zlib.crc32(img.tobytes())
        t0 = time.perf_counter()
        res = best = None
        first_finish = None
        n, total_tokens = 0, 0
        decode_s: float | None = 0.0
        ladder = attempts(self.profile, self.temperature, self.repetition_penalty)
        for temperature, penalty in ladder:
            kwargs: dict = {"max_tokens": self.max_tokens, "temperature": temperature}
            if penalty:
                kwargs["repetition_penalty"] = penalty
            if temperature > 0:
                mx.random.seed(seed + n)
            images: Any = [img]  # mlx-vlm accepts PIL images; its hint says paths only
            res = generate(
                self._model, self._processor, prompt, image=images, verbose=False, **kwargs
            )
            # Every attempt counts: a max_tokens loop that a retry fixed is still a
            # truncation, and its tokens and time are real work.
            n += 1
            first_finish = res.finish_reason if first_finish is None else first_finish
            total_tokens += res.generation_tokens
            step = _decode_seconds(res)
            decode_s = None if decode_s is None or step is None else decode_s + step
            if any(c.isalnum() for c in self.profile.postprocess(res.text)):
                best = res  # keep the latest attempt that has text, in case none is valid
            if acceptable(self.profile, res.text, res.finish_reason):
                best = res
                break
        chosen = best or res
        assert chosen is not None
        return OcrResult(
            text=self.profile.postprocess(chosen.text),
            seconds=time.perf_counter() - t0,
            gen_tokens=chosen.generation_tokens,
            finish_reason=chosen.finish_reason,
            peak_memory_gb=getattr(chosen, "peak_memory", None),
            attempts=n,
            first_finish_reason=first_finish,
            total_gen_tokens=total_tokens,
            gen_seconds=decode_s,
            raw_text=chosen.text,
        )
