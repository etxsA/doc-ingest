"""One JSON POST with retries, shared by the embedder and the reranker clients."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from docingest.domain.errors import DocingestError

MAX_BACKOFF_S = 60.0


class RemoteServiceError(DocingestError):
    """A model server was unreachable, refused the request or answered nonsense."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the server answered, else None


def retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


class JsonClient:
    """Stateless apart from the connection pool; safe to share between threads."""

    def __init__(
        self,
        *,
        label: str,
        timeout_s: float,
        retries: int,
        api_key_env: str | None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: float = 1.0,
    ):
        self.label = label
        self.retries = retries
        self.api_key_env = api_key_env
        self.sleep = sleep
        self.backoff_s = backoff_s
        # No redirects: a 3xx is an error, not a replay of the request (and its key) elsewhere.
        self._http = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False)

    def post(self, url: str, body: dict[str, Any]) -> Any:
        headers = self._headers()
        attempt = 0
        while True:
            try:
                response = self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:  # refused, reset, timed out
                if attempt < self.retries:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise RemoteServiceError(
                    f"{self.label} {url} unreachable after {attempt + 1} attempt(s): {e!r}"
                ) from e
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as e:
                    raise RemoteServiceError(
                        f"{self.label} {url} answered non-JSON: {response.text[:200]!r}"
                    ) from e
            if retryable(response.status_code) and attempt < self.retries:
                self._backoff(attempt)
                attempt += 1
                continue
            hint = " (check api_key_env)" if response.status_code in (401, 403) else ""
            raise RemoteServiceError(
                f"{self.label} {url} answered HTTP {response.status_code}{hint}: "
                f"{response.text.strip()[:300]}",
                status=response.status_code,
            )

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        if not self.api_key_env:
            return {}
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RemoteServiceError(
                f"{self.label}: api_key_env names {self.api_key_env!r}, not set"
            )
        return {"Authorization": f"Bearer {key}"}

    def _backoff(self, attempt: int) -> None:
        self.sleep(min(self.backoff_s * 2**attempt, MAX_BACKOFF_S))
