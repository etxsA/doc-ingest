"""Pipeline configuration, loaded from ``config/pipeline.toml``.

Every field that changes the output participates in ``config_hash`` so cached
results are invalidated automatically when a model revision or threshold moves.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from pydantic import BaseModel, model_validator

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"


class ProbeConfig(BaseModel):
    min_chars: int = 50  # fewer embedded chars than this -> treat as scanned
    image_coverage: float = 0.6  # big image + little text -> scanned
    image_coverage_max_chars: int = 400
    max_garbage_ratio: float = 0.10  # broken font encodings -> re-OCR
    min_alpha_ratio: float = 0.5  # olmOCR heuristic: mostly non-letters -> bad text layer


DEFAULT_OCR_REPO = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
DEFAULT_OCR_REVISION = "2fd8dacbdb8f1e54b8c005f081ec5bf79c56376b"


class OcrConfig(BaseModel):
    repo_id: str = DEFAULT_OCR_REPO
    # Must be a commit of ``repo_id``; change both together. Never inherited across repos.
    revision: str | None = None
    dpi: int = 150
    max_side: int = 1600  # longest image side fed to the VLM (px)
    max_tokens: int = 4096
    temperature: float = 0.0
    repetition_penalty: float | None = 1.05
    prompt: str = (
        "Transcribe this document page to clean Markdown. Preserve reading order, "
        "headings (#), paragraphs, lists, and tables (Markdown tables). Render math "
        "as LaTeX ($...$). Omit page headers/footers and line-break hyphenation. "
        "Output only the transcription, no commentary."
    )

    @model_validator(mode="after")
    def _pin(self) -> OcrConfig:
        if self.revision is None and self.repo_id == DEFAULT_OCR_REPO:
            self.revision = DEFAULT_OCR_REVISION
        if self.revision is None:
            raise ValueError(
                f"[ocr] revision is required for {self.repo_id!r}: pin a commit sha of that repo"
            )
        return self


class QaConfig(BaseModel):
    """Models for the PaperQA2 step. They do not affect ingestion output (not hashed)."""

    llm_repo_id: str | None = None  # None -> reuse the pinned [ocr] model
    llm_revision: str | None = None
    embedding_repo_id: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_revision: str = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    # all-MiniLM-L6-v2 truncates at 256 tokens: chunks target ~900 chars and any chunk
    # still over the window is re-split by tokens before embedding (qa._token_windows).
    chunk_chars: int = 900
    overlap: int = 100
    evidence_k: int = 10


class PipelineConfig(BaseModel):
    probe: ProbeConfig = ProbeConfig()
    ocr: OcrConfig = OcrConfig()
    qa: QaConfig = QaConfig()
    output_dir: str = "data/normalized"

    def hash(self) -> str:
        payload = json.dumps(
            {"probe": self.probe.model_dump(), "ocr": self.ocr.model_dump()},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]


def load_config(path: Path | None = None) -> PipelineConfig:
    path = path or DEFAULT_CONFIG
    if not path.exists():
        return PipelineConfig()
    with path.open("rb") as f:
        return PipelineConfig.model_validate(tomllib.load(f))
