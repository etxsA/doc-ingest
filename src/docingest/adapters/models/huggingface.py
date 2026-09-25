"""Resolve pinned Hugging Face models to local snapshot paths (offline-first)."""

from __future__ import annotations


def resolve(repo_id: str, revision: str) -> str:
    from huggingface_hub import snapshot_download

    try:  # pinned revision already in the local HF cache: no network
        return snapshot_download(repo_id, revision=revision, local_files_only=True)
    except Exception:
        return snapshot_download(repo_id, revision=revision)
