"""The entry points: ``(AppConfig) -> adapter`` factories that docingest calls.

Each reads its own table of ``pipeline.toml`` (``[embedder]``, ``[reranker]``).
"""

from __future__ import annotations

from docingest.config import AppConfig

from .embedder import OpenAICompatibleEmbedder
from .reranker import VllmReranker
from .settings import EmbedderSettings, RerankerSettings, load


def embedder(cfg: AppConfig) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(load(cfg, "embedder", EmbedderSettings))


def reranker(cfg: AppConfig) -> VllmReranker:
    return VllmReranker(load(cfg, "reranker", RerankerSettings))
