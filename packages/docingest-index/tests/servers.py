"""Scripted model servers behind httpx.MockTransport: no socket, no model.

They parse each request the way a strict server would and keep it, so tests can compare the
body with the one the measured end-to-end scripts sent.
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from collections.abc import Callable
from typing import Any

import httpx
import numpy as np


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class _Server:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.replies: list[httpx.Response | Exception] = []  # scripted answers, used first
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return self.answer(request, body)

    def answer(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        raise NotImplementedError


class EmbeddingServer(_Server):
    """``/v1/embeddings``: a hashed bag of words, in the encoding the request asks for."""

    DIMS = 16

    def __init__(self, dims: int = DIMS, shuffle: bool = False) -> None:
        super().__init__()
        self.dims = dims
        self.shuffle = shuffle  # answer each batch in reverse order, as a server may

    def vector(self, text: str) -> np.ndarray:
        v = np.zeros(self.dims, dtype=np.float32)
        for w in words(text):
            v[zlib.crc32(w.encode()) % self.dims] += 1.0
        return v

    def answer(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert isinstance(body["input"], list) and all(isinstance(t, str) for t in body["input"])
        data = []
        for i, text in enumerate(body["input"]):
            v = self.vector(text)
            if body["encoding_format"] == "base64":
                embedding: Any = base64.b64encode(v.tobytes()).decode()
            else:
                assert body["encoding_format"] == "float"
                embedding = v.tolist()
            data.append({"object": "embedding", "index": i, "embedding": embedding})
        if self.shuffle:
            data.reverse()
        usage = {"prompt_tokens": 3 * len(data), "total_tokens": 3 * len(data)}
        return httpx.Response(200, json={"object": "list", "data": data, "usage": usage})


class RerankServer(_Server):
    """``/rerank``: the share of the query's words a document contains, best first."""

    def answer(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert request.url.path == "/rerank"
        wanted = set(words(body["query"]))
        results = [
            {
                "index": i,
                "document": {"text": doc},
                "relevance_score": len(wanted & set(words(doc))) / len(wanted) if wanted else 0.0,
            }
            for i, doc in enumerate(body["documents"])
        ]
        results.sort(key=lambda r: -r["relevance_score"])  # vLLM sorts by score
        return httpx.Response(200, json={"id": "rerank-1", "results": results})


def json_response(status: int, body: Any, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def no_sleep() -> tuple[list[float], Callable[[float], None]]:
    """A ``sleep`` that records the pauses instead of waiting."""
    pauses: list[float] = []
    return pauses, pauses.append
