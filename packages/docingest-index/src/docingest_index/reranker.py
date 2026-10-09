"""Reranker adapter: vLLM's ``/rerank`` endpoint (Qwen3-Reranker as a score model).

One request per call: ``{"model", "query", "documents", "instruction"}``. The server applies
the model's template to the instruction; documents are the chunk texts as they are. The scores
come back as ``results[i].index`` and ``relevance_score`` and are returned in input order.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

import httpx
from docingest.ports import Chunk

from .http import JsonClient, RemoteServiceError
from .settings import RerankerSettings


class VllmReranker:
    def __init__(
        self,
        settings: RerankerSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.url = settings.base_url.rstrip("/") + "/rerank"
        self._client = JsonClient(
            label="reranker server",
            timeout_s=settings.timeout_s,
            retries=settings.retries,
            api_key_env=settings.api_key_env,
            transport=transport,
            sleep=sleep,
        )

    def rerank(self, question: str, chunks: Sequence[Chunk]) -> list[float]:
        if not chunks:
            return []
        reply = self._client.post(
            self.url,
            {
                "model": self.settings.served_model,
                "query": question,
                "documents": [c.text for c in chunks],
                "instruction": self.settings.instruction,
            },
        )
        scores = [0.0] * len(chunks)
        try:
            for item in reply["results"]:
                position = item["index"]
                if not 0 <= position < len(scores):
                    raise IndexError(position)
                scores[position] = float(item["relevance_score"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise RemoteServiceError(
                f"reranker server {self.url} sent a reply without usable results: "
                f"{str(reply)[:200]}"
            ) from e
        return scores
