"""Embedder adapter: any OpenAI-compatible ``/v1/embeddings`` server (vLLM serving Qwen3).

Requests are the ones of the measured retrieval: documents in batches of 64 with
``encoding_format: "base64"``, eight requests in flight; a question as one input with
``encoding_format: "float"``. Every vector is L2-normalized in float32 (``v / (|v| + 1e-12)``),
so cosine similarity is a dot product.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import numpy as np
from docingest.ports import Vector

from .http import JsonClient, RemoteServiceError
from .settings import EmbedderSettings

NORM_EPS = 1e-12
# Qwen's own task for web search, a convenient non-empty choice for query_instruction.
QUERY_PRESETS = {
    "web": "Given a web search query, retrieve relevant passages that answer the query",
}


def query_task(instruction: str) -> str:
    """The task text of a ``query_instruction`` setting (a preset name or the text itself)."""
    return QUERY_PRESETS.get(instruction, instruction)


def format_query(instruction: str, text: str) -> str:
    """Qwen3-Embedding's query format; without an instruction the question as it is."""
    task = query_task(instruction)
    return f"Instruct: {task}\nQuery:{text}" if task else text


def embedder_fingerprint(settings: EmbedderSettings) -> str:
    """Everything that changes the vectors of documents. The query instruction is not in it
    (it changes query vectors only), nor are the URL, the served name and the transport
    settings: the server decides which weights answer, ``model@revision`` names them (and so
    the vector length, which the index records when it sees it)."""
    return "openai-compatible " + json.dumps(
        {
            "model": settings.model,
            "revision": settings.revision,
            "normalize": "l2",
        },
        sort_keys=True,
    )


def normalize(matrix: np.ndarray) -> np.ndarray:
    """Rows divided by their norm, in float32, exactly as the measured retrieval did."""
    m = np.asarray(matrix, dtype=np.float32)
    m /= np.linalg.norm(m, axis=-1, keepdims=True) + np.float32(NORM_EPS)
    return m


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        settings: EmbedderSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.url = settings.base_url.rstrip("/") + "/embeddings"
        self.fingerprint = embedder_fingerprint(settings)
        self.query_instruction = query_task(settings.query_instruction)
        self._client = JsonClient(
            label="embedding server",
            timeout_s=settings.timeout_s,
            retries=settings.retries,
            api_key_env=settings.api_key_env,
            transport=transport,
            sleep=sleep,
        )

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        size = self.settings.batch_size
        starts = range(0, len(texts), size)

        def one(start: int) -> np.ndarray:
            batch = list(texts[start : start + size])
            reply = self._client.post(
                self.url,
                {"model": self.settings.served_model, "input": batch, "encoding_format": "base64"},
            )
            return self._matrix(reply, len(batch))

        workers = min(self.settings.concurrency, len(starts))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(one, starts))  # in order, whatever finishes first
        if len({part.shape[1] for part in parts}) != 1:
            raise RemoteServiceError(
                f"embedding server {self.url} sent vectors of different lengths"
            )
        return normalize(np.vstack(parts)).tolist()

    def embed_query(self, text: str) -> Vector:
        reply = self._client.post(
            self.url,
            {
                "model": self.settings.served_model,
                "input": [format_query(self.settings.query_instruction, text)],
                "encoding_format": "float",
            },
        )
        return normalize(self._matrix(reply, 1))[0].tolist()

    def _matrix(self, reply: Any, expected: int) -> np.ndarray:
        """The vectors of one reply in input order, as float32."""
        try:
            items = sorted(reply["data"], key=lambda item: item["index"])
            if [item["index"] for item in items] != list(range(expected)):
                raise ValueError("indexes do not cover the inputs")
            rows = [self._decode(item["embedding"]) for item in items]
            matrix = np.vstack(rows)
        except (KeyError, TypeError, ValueError) as e:
            raise RemoteServiceError(
                f"embedding server {self.url} sent a reply that is not {expected} embedding(s): "
                f"{str(reply)[:200]} ({e})"
            ) from e
        return matrix

    @staticmethod
    def _decode(embedding: Any) -> np.ndarray:
        if isinstance(embedding, str):  # base64 of little-endian float32
            return np.frombuffer(base64.b64decode(embedding), dtype=np.float32)
        return np.asarray(embedding, dtype=np.float32)  # a server that ignores encoding_format
