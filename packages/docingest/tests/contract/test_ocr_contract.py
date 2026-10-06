"""OcrEngine contract: every OCR implementation must pass the same tests.

The in-memory fake, the HTTP adapter (against a tiny local OpenAI-compatible stub)
and the real in-process MLX model (opt in: DOCINGEST_MODEL_TESTS=1).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fakes import FakeOcr
from PIL import Image, ImageDraw

from docingest.adapters.ocr.mlx_vlm import MlxVlmOcr
from docingest.adapters.ocr.openai_compat import OpenAICompatibleOcr
from docingest.adapters.ocr.profiles import profile_for
from docingest.domain.models import ModelRef
from docingest.ports import OcrEngine, OcrResult

# Small, pinned model: enough to prove the in-process adapter honours the contract.
MLX_MODEL = ModelRef(
    repo_id="mlx-community/Qwen3-VL-2B-Instruct-4bit",
    revision="9c4f5209e57b31f4b9dfba735de3fb983739c9cc",
)


class _StubChat(BaseHTTPRequestHandler):
    """Minimal /v1/chat/completions: echoes a fixed transcription."""

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.dumps(
            {
                "choices": [
                    {"message": {"content": "# Stub page\n\nText"}, "finish_reason": "stop"}
                ],
                "usage": {"completion_tokens": 5},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def stub_server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubChat)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@pytest.fixture(
    scope="module",
    params=["fake", "openai-compatible", pytest.param("mlx-vlm", marks=pytest.mark.model)],
)
def make_engine(request) -> Iterator[Callable[[], OcrEngine]]:
    """A factory, so tests can build two engines with identical settings."""
    if request.param == "fake":
        yield FakeOcr
    elif request.param == "openai-compatible":
        with stub_server() as url:
            yield lambda: OpenAICompatibleOcr(
                ModelRef(repo_id="org/vl-model", revision="a" * 40),
                profile_for("markdown"),
                base_url=url,
                backoff_s=0,
            )
    else:
        yield lambda: MlxVlmOcr(MLX_MODEL, profile_for("markdown"))


@pytest.fixture(scope="module")
def engine(make_engine) -> Iterator[OcrEngine]:
    e = make_engine()
    yield e
    if unload := getattr(e, "unload", None):  # free multi-GB weights between params
        unload()


def page(mode: str = "RGB") -> Image.Image:
    img = Image.new("RGB", (640, 200), "white")
    ImageDraw.Draw(img).text((20, 80), "Hello contract test", fill="black")
    return img.convert(mode)


def test_satisfies_the_port(engine):
    assert isinstance(engine, OcrEngine)
    assert isinstance(engine.model, ModelRef) and engine.model.revision
    assert isinstance(engine.dpi, int) and engine.dpi > 0
    assert isinstance(engine.fingerprint, str) and engine.fingerprint


def test_transcribe_returns_an_ocr_result(engine):
    res = engine.transcribe(page())
    assert isinstance(res, OcrResult)
    assert isinstance(res.text, str)
    assert res.seconds >= 0
    assert isinstance(res.gen_tokens, int) and res.gen_tokens >= 0
    assert res.finish_reason is None or isinstance(res.finish_reason, str)


@pytest.mark.parametrize("mode", ["L", "RGBA"])
def test_accepts_any_image_mode(engine, mode):
    assert isinstance(engine.transcribe(page(mode)).text, str)


def test_fingerprint_is_stable(make_engine, engine):
    before = engine.fingerprint
    engine.transcribe(page())
    assert engine.fingerprint == before  # transcribing must not change the cache key
    assert make_engine().fingerprint == before  # same settings -> same key
