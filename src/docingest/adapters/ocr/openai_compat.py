"""OcrEngine adapter: any OpenAI-compatible vision chat endpoint, over plain HTTP.

vLLM, LM Studio, Ollama (``/v1``), ``mlx_vlm.server`` and hosted APIs all accept
``POST {base_url}/chat/completions`` with an ``image_url`` content part, so this one
client lets a Linux / CI box without MLX OCR against a GPU server, and shows the OCR
port is swapped by config alone. It reuses the MLX adapter's profiles and image
fitting, so a model gets the same prompt, input size and clean-up whichever runtime
serves it. Stdlib only (urllib): an SDK would be a dependency for one endpoint.

Reproducibility: the server decides which weights answer. Point ``served_model`` at
the pinned snapshot (for ``mlx_vlm.server``, the local snapshot path of
``repo_id@revision``); here ``repo_id``/``revision`` only label the output.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, override

from PIL import Image

from ...domain.errors import OcrError
from ...domain.models import ModelRef
from ...ports import OcrResult
from .mlx_vlm import fit_image
from .profiles import OcrProfile, acceptable, attempts

if TYPE_CHECKING:
    from ...config import OcrConfig

DEFAULT_TIMEOUT_S = 600.0  # one full page at a few tokens/s on a slow server
DEFAULT_RETRIES = 3
MAX_BACKOFF_S = 60.0


class OcrServerError(OcrError):
    """The OCR server was unreachable, refused the request or answered nonsense."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the server answered, else None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # urllib would replay the Authorization header to wherever a redirect points and
    # turn the POST into a GET; surface the 3xx as an error instead.
    @override
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


def _png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")  # lossless: JPEG artefacts hurt small glyphs
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _error_detail(err: urllib.error.HTTPError) -> str:
    try:
        raw = err.read().decode("utf-8", "replace")
    except Exception:
        return err.reason or ""
    finally:
        err.close()  # frees the connection before a retry
    try:  # OpenAI shape: {"error": {"message": ...}}; FastAPI: {"detail": ...}
        body = json.loads(raw)
        if isinstance(body, dict):
            inner = body.get("error", body.get("detail", body))
            raw = str(inner.get("message", inner) if isinstance(inner, dict) else inner)
    except ValueError:
        pass
    return raw.strip()[:300]


def _first_choice(reply: Any) -> dict[str, Any]:
    try:
        choice = reply["choices"][0]
        if not isinstance(choice["message"], dict):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        raise OcrServerError(
            f"OCR server reply has no choices[0].message: {str(reply)[:300]}"
        ) from None
    return choice


def _content_text(choice: dict[str, Any]) -> str:
    content = choice["message"].get("content")
    if isinstance(content, list):  # some servers answer with content parts
        return "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    return content or ""


class OpenAICompatibleOcr:
    """Stateless HTTP client: construction is free, each page is one request (or a few)."""

    def __init__(
        self,
        model: ModelRef,
        profile: OcrProfile,
        *,
        base_url: str,
        served_model: str | None = None,
        api_key_env: str | None = None,
        dpi: int = 150,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        repetition_penalty: float | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        backoff_s: float = 1.0,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"[ocr] base_url must be an http(s) URL, got {base_url!r}")
        self.model = model
        self.profile = profile
        self.dpi = dpi
        self.base_url = base_url.rstrip("/")
        self.served_model = served_model or model.repo_id
        self.api_key_env = api_key_env  # the variable's name; the key is read per request
        # None -> the profile's model-card defaults
        self.max_tokens = max_tokens or profile.max_tokens
        self.temperature = temperature
        self.repetition_penalty = (
            profile.repetition_penalty if repetition_penalty is None else repetition_penalty
        )
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        # Transport settings (timeout, retries, key) cannot change the text: not included.
        self.fingerprint = "openai-compatible " + json.dumps(
            {
                "base_url": self.base_url,
                "served_model": self.served_model,
                "model": f"{model.repo_id}@{model.revision}",
                "profile": profile.name,
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
        self._opener = urllib.request.build_opener(_NoRedirects)

    @classmethod
    def from_config(cls, cfg: OcrConfig, profile: OcrProfile) -> OpenAICompatibleOcr:
        assert cfg.revision is not None  # OcrConfig refuses unpinned models
        return cls(
            ModelRef(repo_id=cfg.repo_id, revision=cfg.revision),
            profile,
            base_url=cfg.base_url,
            served_model=cfg.served_model,
            api_key_env=cfg.api_key_env,
            dpi=cfg.dpi,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            repetition_penalty=cfg.repetition_penalty,
            # Optional [ocr] transport keys: defaults when OcrConfig does not define them.
            timeout_s=getattr(cfg, "timeout_s", None) or DEFAULT_TIMEOUT_S,
            retries=getattr(cfg, "retries", DEFAULT_RETRIES),
        )

    def transcribe(self, image: Image.Image) -> OcrResult:
        data_url = _png_data_url(fit_image(image, self.profile.max_side))
        t0 = time.perf_counter()
        choice: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        finishes: list[str | None] = []
        total_tokens = 0
        best: tuple[dict[str, Any], dict[str, Any]] | None = None
        for temperature, penalty in attempts(
            self.profile, self.temperature, self.repetition_penalty
        ):
            reply = self._post(self._body(data_url, temperature, penalty))
            choice = _first_choice(reply)
            usage = reply.get("usage") or {}
            finishes.append(choice.get("finish_reason"))
            total_tokens += int(usage.get("completion_tokens") or 0)
            raw = _content_text(choice)
            if any(c.isalnum() for c in self.profile.postprocess(raw)):
                best = (choice, usage)  # latest attempt with text, in case none is valid
            if acceptable(self.profile, raw, choice.get("finish_reason")):
                best = (choice, usage)
                break
        if best is not None:
            choice, usage = best
        return OcrResult(
            text=self.profile.postprocess(_content_text(choice)),
            seconds=time.perf_counter() - t0,
            gen_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choice.get("finish_reason"),
            # The whole ladder, so a benchmark sees a loop that a retry fixed. Decode time
            # is not observable over HTTP: gen_seconds stays None.
            attempts=len(finishes),
            first_finish_reason=finishes[0] if finishes else None,
            total_gen_tokens=total_tokens,
            raw_text=_content_text(choice),
        )

    def _content(self, data_url: str) -> list[dict[str, Any]]:
        image = {"type": "image_url", "image_url": {"url": data_url}}
        text = {"type": "text", "text": self.profile.prompt}
        # Same order the model was trained with (olmOCR: text first); default image first.
        return [text, image] if self.profile.prompt_first else [image, text]

    def _body(self, data_url: str, temperature: float, penalty: float | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.served_model,
            "messages": [
                {
                    "role": "user",
                    "content": self._content(data_url),
                }
            ],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        # Not in the OpenAI spec; vLLM, mlx_vlm.server, LM Studio honour it and most
        # others ignore it. 1.0 means "off", so it is omitted (strict cloud APIs).
        if penalty and penalty != 1.0:
            body["repetition_penalty"] = penalty
        if self.profile.chat_kwargs:
            # vLLM / SGLang read chat_template_kwargs; mlx_vlm.server reads top-level
            # keys such as enable_thinking.
            body["chat_template_kwargs"] = dict(self.profile.chat_kwargs)
            for key, value in self.profile.chat_kwargs.items():
                body.setdefault(key, value)
        return body

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise OcrServerError(
                    f"[ocr] api_key_env names {self.api_key_env!r}, which is not set"
                )
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _post(self, body: dict[str, Any]) -> Any:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(body).encode()
        headers = self._headers()
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with self._opener.open(request, timeout=self.timeout_s) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as e:
                detail = _error_detail(e)
                if _retryable(e.code) and attempt < self.retries:
                    self._backoff(attempt, e.headers.get("Retry-After"))
                    attempt += 1
                    continue
                hint = " (check [ocr] api_key_env)" if e.code in (401, 403) else ""
                raise OcrServerError(
                    f"OCR server {url} answered HTTP {e.code}{hint}: {detail}", status=e.code
                ) from e
            except (OSError, http.client.HTTPException) as e:  # refused, reset, timed out
                reason = getattr(e, "reason", None) or e
                # A timed-out generation may still be running server-side: resending
                # would only queue a duplicate behind it.
                timed_out = isinstance(reason, TimeoutError)
                if not timed_out and attempt < self.retries:
                    self._backoff(attempt, None)
                    attempt += 1
                    continue
                what = (
                    f"timed out after {self.timeout_s:g}s"
                    if timed_out
                    else f"unreachable after {attempt + 1} attempt(s): {reason}"
                )
                raise OcrServerError(f"OCR server {url} {what}") from e
            try:
                return json.loads(raw)
            except ValueError as e:
                raise OcrServerError(f"OCR server {url} answered non-JSON: {raw[:200]!r}") from e

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.backoff_s * 2**attempt
        if retry_after and retry_after.strip().isdigit():  # seconds form only
            delay = max(delay, float(retry_after))
        time.sleep(min(delay, MAX_BACKOFF_S))
