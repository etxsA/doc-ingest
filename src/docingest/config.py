"""Pipeline configuration, loaded from ``config/pipeline.toml``.

Every field that changes the output participates in ``config_hash`` so cached
results are invalidated automatically when a model revision or threshold moves.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from pydantic import BaseModel

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"


class ProbeConfig(BaseModel):
    min_chars: int = 50  # fewer embedded chars than this -> treat as scanned
    image_coverage: float = 0.6  # big image + little text -> scanned
    image_coverage_max_chars: int = 400
    max_garbage_ratio: float = 0.10  # broken font encodings -> re-OCR
    min_alpha_ratio: float = 0.5  # olmOCR heuristic: mostly non-letters -> bad text layer


class OcrConfig(BaseModel):
    repo_id: str = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
    revision: str = "2fd8dacbdb8f1e54b8c005f081ec5bf79c56376b"
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


class PipelineConfig(BaseModel):
    probe: ProbeConfig = ProbeConfig()
    ocr: OcrConfig = OcrConfig()
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
