"""Resolve pinned Hugging Face models to local snapshot paths (offline-first)."""

from __future__ import annotations

from .config import PipelineConfig


def resolve(repo_id: str, revision: str) -> str:
    from huggingface_hub import snapshot_download

    try:  # pinned revision already in the local HF cache: no network
        return snapshot_download(repo_id, revision=revision, local_files_only=True)
    except Exception:
        return snapshot_download(repo_id, revision=revision)


def llm_path(cfg: PipelineConfig) -> str:
    repo = cfg.qa.llm_repo_id or cfg.ocr.repo_id
    rev = cfg.qa.llm_revision or (cfg.ocr.revision if repo == cfg.ocr.repo_id else None)
    if rev is None:
        raise ValueError(f"[qa] llm_revision is required for {repo!r}")
    return resolve(repo, rev)


def embedding_path(cfg: PipelineConfig) -> str:
    return resolve(cfg.qa.embedding_repo_id, cfg.qa.embedding_revision)
