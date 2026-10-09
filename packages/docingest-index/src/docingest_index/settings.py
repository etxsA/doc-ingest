"""The ``[index]``, ``[embedder]`` and ``[reranker]`` tables of ``pipeline.toml``.

docingest keeps tables it does not know in ``AppConfig.model_extra``; this package owns these
three and validates them (unknown keys are errors, so a typo is not silently ignored).
"""

from __future__ import annotations

from docingest.config import AppConfig, IndexConfig
from docingest.ports import KeywordMode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# The measured pair (D-161): both pinned to the commit the experiments used.
DEFAULT_EMBEDDER_MODEL = "Qwen/Qwen3-Embedding-4B"
DEFAULT_EMBEDDER_REVISION = "5cf2132abc99cad020ac570b19d031efec650f2b"
DEFAULT_RERANKER_MODEL = "Qwen/Qwen3-Reranker-8B"
DEFAULT_RERANKER_REVISION = "77d193c791ed757ca307ee72715aa132723da912"
DEFAULT_RERANK_INSTRUCTION = (
    "Given a question about a research paper, retrieve the passage that answers it"
)

# The one place for the default of [index] max_chunks_per_paper. 0 means no cap, which is what
# the measured end-to-end retrieval did.
DEFAULT_MAX_CHUNKS_PER_PAPER = 0


class _Table(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexSettings(IndexConfig):
    """All of ``[index]``. ``candidates`` and ``contexts`` come from docingest's ``IndexConfig``
    (``ask`` reads them there, and their defaults are written once); unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")

    dir: str = "data/index"  # relative to the working directory, like output_dir
    # Fuse BM25 with the dense ranking (RRF) for questions that look English ("english"),
    # for every question ("always") or never.
    bm25: KeywordMode = "english"
    max_chunks_per_paper: int = Field(default=DEFAULT_MAX_CHUNKS_PER_PAPER, ge=0)  # 0: no cap


class EmbedderSettings(_Table):
    base_url: str = "http://127.0.0.1:8002/v1"
    served_model: str = "qwen-embed"  # the model name the server expects in each request
    model: str = DEFAULT_EMBEDDER_MODEL  # recorded in the fingerprint, not sent
    revision: str | None = None  # a commit of ``model``; required for any other model
    # "" sends questions as they are. A task description is sent as
    # "Instruct: <task>\nQuery:<question>" (the Qwen3-Embedding format); "web" is Qwen's
    # web-search task. Not part of the index folder key.
    query_instruction: str = ""
    api_key_env: str | None = None  # name of the environment variable holding the key
    batch_size: int = Field(default=64, ge=1)  # texts per request
    concurrency: int = Field(default=8, ge=1)  # requests in flight
    timeout_s: float = Field(default=600.0, gt=0)
    retries: int = Field(default=5, ge=0)  # on 408/429/5xx and connection errors

    @model_validator(mode="after")
    def _pin(self) -> EmbedderSettings:
        if self.revision is None and self.model == DEFAULT_EMBEDDER_MODEL:
            self.revision = DEFAULT_EMBEDDER_REVISION
        if self.revision is None:
            raise ValueError(
                f"revision is required for {self.model!r}: pin a commit sha of that repo"
            )
        return self


class RerankerSettings(_Table):
    base_url: str = "http://127.0.0.1:8003"
    served_model: str = "qwen-rerank"
    model: str = DEFAULT_RERANKER_MODEL
    revision: str | None = None
    instruction: str = DEFAULT_RERANK_INSTRUCTION  # the task, sent with every request
    api_key_env: str | None = None
    timeout_s: float = Field(default=600.0, gt=0)
    retries: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _pin(self) -> RerankerSettings:
        if self.revision is None and self.model == DEFAULT_RERANKER_MODEL:
            self.revision = DEFAULT_RERANKER_REVISION
        if self.revision is None:
            raise ValueError(
                f"revision is required for {self.model!r}: pin a commit sha of that repo"
            )
        return self


def load[T: BaseModel](cfg: AppConfig, table: str, model: type[T]) -> T:
    """The settings of ``[<table>]``; a missing table means the defaults."""
    raw = (cfg.model_extra or {}).get(table, {})
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'table'}: {err['msg']}" for err in e.errors()
        )
        raise ValueError(f"[{table}] {problems}") from e
