"""Application configuration, loaded from ``config/pipeline.toml``.

``[adapters]`` picks the implementation plugged into each port (see
``docingest.bootstrap.REGISTRY``); the other sections configure those adapters.
Unknown top-level sections are kept (``AppConfig.model_extra``) so third-party
adapter plugins can carry their own settings in the same file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain.routing import RoutingPolicy

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"
DEFAULT_OCR_REPO = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
DEFAULT_OCR_REVISION = "2fd8dacbdb8f1e54b8c005f081ec5bf79c56376b"


class AdapterSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detector: str = "magic"
    pdf: str = "pdfium"
    ocr: str = "mlx-vlm"
    images: str = "pillow"
    office: str = "docling"
    latex: str = "pandoc"
    text: str = "passthrough"
    store: str = "filesystem"
    qa: str = "paperqa"
    crawler: str = "arxiv"


class OcrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str = DEFAULT_OCR_REPO
    # Must be a commit of ``repo_id``; change both together. Never inherited across repos.
    revision: str | None = None
    profile: str = "markdown"  # prompt + image size + clean-up, see adapters/ocr/profiles.py
    prompt: str | None = None  # override the profile's prompt
    max_side: int | None = None  # override the profile's image size
    dpi: int = 150
    max_tokens: int | None = None  # None -> the profile's model-card default
    temperature: float = 0.0
    repetition_penalty: float | None = None  # None -> the profile's default
    # Only for the "openai-compatible" OCR adapter (vLLM, LM Studio, mlx_vlm.server, ...):
    base_url: str = "http://127.0.0.1:8080/v1"
    served_model: str | None = None  # model id the server expects; default: repo_id
    api_key_env: str | None = None  # name of the env var holding the key, never the key

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
    """PaperQA2 step. It does not affect ingestion output (not in any cache key)."""

    model_config = ConfigDict(extra="forbid")

    # Any litellm model string, e.g. "ollama/llama3.1". None -> the pinned [ocr] model
    # served locally by scripts/serve_llm.sh (mlx_vlm.server, OpenAI-compatible).
    llm: str | None = None
    llm_base: str = "http://127.0.0.1:8080/v1"
    llm_revision: str | None = None  # only used when llm is None and differs from [ocr]
    embedding_repo_id: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_revision: str = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    embedding: str | None = None  # any PaperQA embedding string overrides the pinned one
    # all-MiniLM-L6-v2 truncates at 256 tokens: chunks target ~900 chars and any chunk
    # still over the window is re-split by tokens before embedding.
    chunk_chars: int = 900
    overlap: int = 100
    evidence_k: int = 10
    answer_max_sources: int = 3
    max_concurrent_requests: int = 2


class LatexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: int = 120  # pandoc wall-clock limit per document (pandoc can hang on \input loops)
    max_archive_mb: int = 200  # refuse larger extracted sources (zip-bomb guard)
    split_level: int = 2  # section depth that becomes one segment ("page")
    # None -> the pandoc bundled by pypandoc-binary (pinned by uv.lock). Set e.g.
    # "/opt/homebrew/bin/pandoc" for a native arm64 build.
    pandoc_path: str | None = None
    fallback: bool = True  # pylatexenc plain-text fallback when pandoc fails


class ArxivConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str = "https://export.arxiv.org/api/query"
    eprint_url: str = "https://arxiv.org/e-print/{id}"
    pdf_url: str = "https://arxiv.org/pdf/{id}"
    delay_s: float = 3.0  # arXiv API terms: at most one request every 3 seconds
    timeout_s: float = 60.0
    retries: int = 3
    contact: str | None = None  # optional mailto for the User-Agent, as arXiv asks
    prefer: list[str] = Field(default_factory=lambda: ["latex", "pdf"])
    src_url: str = "https://arxiv.org/src/{id}"  # /e-print/ 301-redirects here
    oai_url: str = "https://oaipmh.arxiv.org/oai"  # OAI-PMH (moved in 2025); has licenses
    fetch_license: bool = True  # one extra OAI-PMH request per paper


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    output_dir: str = "data/normalized"
    raw_dir: str = "data/raw"
    adapters: AdapterSelection = AdapterSelection()
    routing: RoutingPolicy = RoutingPolicy()
    ocr: OcrConfig = OcrConfig()
    qa: QaConfig = QaConfig()
    latex: LatexConfig = LatexConfig()
    arxiv: ArxivConfig = ArxivConfig()


def load_config(path: Path | None = None) -> AppConfig:
    path = path or DEFAULT_CONFIG
    if not path.exists():
        return AppConfig()
    with path.open("rb") as f:
        return AppConfig.model_validate(tomllib.load(f))
