"""Port: an OCR benchmark suite (samples, references, scoring) and the candidates it compares.

A suite owns its file layout: where a candidate's transcription of a sample lives
(``output_path``) and how a finished run directory is scored (``score``). Official
scorers expect exact layouts (olmOCR-bench wants ``<pdf>_pg1_repeat1.md``), so the
benchmark runner never invents output paths itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from PIL.Image import Image

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Sample:
    """One page to transcribe. The image is produced lazily: suites hold hundreds of pages."""

    id: str  # unique within the suite, stable across runs
    category: str  # breakdown key: degradation level, olmOCR-bench test file, ...
    load_image: Callable[[], Image] = field(repr=False, compare=False)
    reference: str | None = None  # ground-truth text, when the suite scores against one
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateSpec:
    """One OCR configuration under test: model x profile x generation settings.

    ``None`` fields fall back to the profile's model-card defaults, exactly as in
    ``[ocr]`` of pipeline.toml, so a candidate means the same thing in both places.
    """

    name: str  # directory name and scorer id: [A-Za-z0-9._-]
    repo_id: str
    revision: str  # commit sha of repo_id
    profile: str = "markdown"
    ocr: str = "mlx-vlm"  # OCR adapter name (see bootstrap.REGISTRY["ocr"])
    max_side: int | None = None
    max_tokens: int | None = None
    dpi: int | None = None
    prompt: str | None = None
    temperature: float = 0.0
    repetition_penalty: float | None = None

    def __post_init__(self) -> None:
        if not _SAFE_NAME.match(self.name) or self.name == "pdfs":
            raise ValueError(f"candidate name {self.name!r} must match [A-Za-z0-9._-]+")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Estimate:
    """A mean with its (bootstrap) confidence interval; ``None`` when nothing was scored."""

    mean: float | None
    low: float | None = None
    high: float | None = None
    n: int = 0


@dataclass
class SuiteScore:
    """Scores of one candidate on one suite."""

    primary: str  # headline metric used for ranking, e.g. "cer" or "pass_rate"
    higher_is_better: bool
    metrics: dict[str, Estimate]  # overall, per metric
    by_category: dict[str, dict[str, Estimate]] = field(default_factory=dict)
    # Per scored unit (a page, an olmOCR-bench test) -> metric values, for paired tests.
    units: dict[str, dict[str, float | None]] = field(default_factory=dict)
    # unit -> group; the headline is the mean of group means (empty: plain mean). Without
    # clusters, units are also resampled within their group.
    unit_groups: dict[str, str] = field(default_factory=dict)
    # unit -> cluster of correlated units that are resampled together: a synthetic page at
    # every degradation level, an olmOCR-bench PDF with all its tests (empty: independent).
    unit_clusters: dict[str, str] = field(default_factory=dict)
    # cluster -> stratum it is resampled within (empty: one stratum)
    cluster_strata: dict[str, str] = field(default_factory=dict)
    n_outputs: int = 0
    n_samples: int = 0
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    # The suite's scoring rules that produced this score. Scoring changes bump it instead
    # of the suite fingerprint: a finished run is re-scored, never re-transcribed.
    scoring_version: int | None = None
    # What was scored (set by the runner's ``score_run``): lets a later report tell a
    # stale score from a current one.
    stamp: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SuiteScore:
        d = dict(d)
        d["metrics"] = {k: Estimate(**v) for k, v in d["metrics"].items()}
        d["by_category"] = {
            cat: {k: Estimate(**v) for k, v in ms.items()}
            for cat, ms in d.get("by_category", {}).items()
        }
        return cls(**d)


@runtime_checkable
class BenchmarkSuite(Protocol):
    """A suite may also expose ``scoring_version: int`` (recorded in its scores)."""

    name: str
    fingerprint: str  # data + rendering + scoring settings; changes invalidate a run

    def samples(self) -> list[Sample]: ...

    def output_path(self, run_dir: Path, candidate: str, sample: Sample) -> Path: ...

    def score(self, run_dir: Path, candidates: list[str]) -> dict[str, SuiteScore]:
        """Score each candidate's outputs found under ``run_dir``."""
        ...
