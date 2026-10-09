"""The entry points: ``(AppConfig) -> adapter`` factories that docingest calls.

Each reads its own table of ``pipeline.toml`` (``[embedder]``, ``[reranker]``, ``[index]``).
The index is built for the corpus ``output_dir`` names, the embedder described by ``[embedder]``
and the chunk settings of ``[qa]``, so it holds the chunks ``docingest ask`` cuts and two
corpora that share ``[index] dir`` get separate folders.
"""

from __future__ import annotations

from pathlib import Path

from docingest.config import AppConfig

from .embedder import OpenAICompatibleEmbedder
from .local import LocalIndex
from .reranker import VllmReranker
from .settings import EmbedderSettings, IndexSettings, RerankerSettings, load


def embedder(cfg: AppConfig) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(load(cfg, "embedder", EmbedderSettings))


def reranker(cfg: AppConfig) -> VllmReranker:
    return VllmReranker(load(cfg, "reranker", RerankerSettings))


def index(cfg: AppConfig) -> LocalIndex:
    settings = load(cfg, "index", IndexSettings)
    return LocalIndex(
        settings.dir,
        corpus=Path(cfg.output_dir).resolve(),  # the index belongs to this corpus
        embedder=load(cfg, "embedder", EmbedderSettings),
        chunk_chars=cfg.qa.chunk_chars,
        overlap=cfg.qa.overlap,
        settings=settings,
    )
