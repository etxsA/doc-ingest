"""Driving adapter: ``docingest bench ...``, a reproducible OCR benchmark harness.

Candidates (model x profile x settings) and suites are declared in
``config/benchmark.toml``. This module is the benchmark's composition root: it builds
the suite adapters from those settings and each candidate's OCR engine through
``bootstrap.build("ocr", ...)`` on a per-candidate copy of the pipeline config, so a
candidate's ``ocr`` adapter name is honored exactly like ``[adapters] ocr``.

Mounted by the main CLI as ``app.add_typer(bench_app, name="bench")``.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, ClassVar

import typer
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..adapters.datasets.olmocr_bench import (
    CATEGORIES,
    REPO_ID,
    OlmOcrBenchSuite,
    ScorerConfig,
    prepare_subset,
    subset_id,
)
from ..adapters.datasets.synthetic import LEVELS, SyntheticSuite
from ..application.benchmark import (
    BenchmarkRunner,
    RunMismatchError,
    load_scores,
    needs_scoring,
    rank,
    read_manifest,
    score_run,
    write_report,
)
from ..bootstrap import build
from ..config import AppConfig, OcrConfig, load_config
from ..ports import BenchmarkSuite, CandidateSpec, OcrEngine, SuiteScore

DEFAULT_BENCH_CONFIG = Path(__file__).resolve().parents[3] / "config" / "benchmark.toml"
SUITES = ("synthetic", "olmocr-bench")
# Current scoring rules per suite: a saved score made under other rules is re-scored by
# ``bench report`` (read from the classes: no suite data is needed to know it).
SCORING_VERSIONS: dict[str, int] = {
    SyntheticSuite.name: SyntheticSuite.scoring_version,
    OlmOcrBenchSuite.name: OlmOcrBenchSuite.scoring_version,
}
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

bench_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Reproducible OCR benchmark: candidates x suites (config/benchmark.toml).",
)
console = Console()


def _log(msg: str) -> None:
    console.print(msg, markup=False, highlight=False)


# --------------------------------------------------------------------------- settings


class SyntheticDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    pages: list[int]  # 0-based


class SyntheticSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[SyntheticDocument]
    levels: list[str] = Field(default_factory=lambda: list(LEVELS))
    dpi: int = 200
    seed: int = 0
    min_ref_chars: int = 200
    primary_metric: str = "cer"


class OlmOcrBenchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The dataset subset and rendering: recorded in the run manifest.
    repo_id: str = REPO_ID
    revision: str
    per_category: int = 20  # 0 = every PDF
    seed: int = 0
    long_side: int = 2048
    categories: list[str] = Field(default_factory=lambda: list(CATEGORIES))
    # The scorer environment: taken from the current config / env when scoring.
    scorer_python: str = ".bench-venv/bin/python"
    playwright_browsers: str | None = ".bench-venv/pw"
    scorer_home: str | None = ".bench-venv/home"
    scorer_timeout_s: float = 3600
    bootstrap_samples: int = 1000
    confidence_level: float = 0.95
    skip_baseline: bool = False

    DEFINITION: ClassVar[tuple[str, ...]] = (
        "repo_id",
        "revision",
        "per_category",
        "seed",
        "long_side",
        "categories",
    )

    def definition(self) -> dict[str, Any]:
        return self.model_dump(include=set(self.DEFINITION))

    def scorer(self) -> ScorerConfig:
        def env(name: str, value: str | None) -> Path | None:
            v = os.environ.get(f"DOCINGEST_BENCH_{name}", value)
            return Path(v).absolute() if v else None

        python = env("SCORER_PYTHON", self.scorer_python)
        assert python is not None
        return ScorerConfig(
            python=python,
            playwright_browsers=env("PLAYWRIGHT_BROWSERS", self.playwright_browsers),
            home=env("SCORER_HOME", self.scorer_home),
            timeout_s=self.scorer_timeout_s,
            bootstrap_samples=self.bootstrap_samples,
            confidence_level=self.confidence_level,
            skip_baseline=self.skip_baseline,
        )


class SuitesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    synthetic: SyntheticSettings | None = None
    olmocr_bench: OlmOcrBenchSettings | None = Field(default=None, alias="olmocr-bench")


class BenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs_dir: str = "data/bench/runs"
    data_dir: str = "data/bench"
    pipeline_config: str | None = None  # base AppConfig for the OCR adapters
    suites: SuitesConfig = SuitesConfig()
    # Scoring-time options (not part of any suite fingerprint): re-scoring a finished run
    # applies them without re-transcription. [scoring.synthetic] report_separately = [...]
    scoring: dict[str, dict[str, Any]] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def specs(self) -> dict[str, CandidateSpec]:
        out = {}
        for c in self.candidates:
            try:
                spec = CandidateSpec(**c)
            except (TypeError, ValueError) as e:
                raise typer.BadParameter(f"invalid candidate {c.get('name')!r}: {e}") from e
            if spec.name in out:
                raise typer.BadParameter(f"duplicate candidate name {spec.name!r}")
            out[spec.name] = spec
        return out

    def suite_settings(self, name: str) -> dict[str, Any]:
        s = self.suites.synthetic if name == "synthetic" else self.suites.olmocr_bench
        if s is None:
            raise typer.BadParameter(f"suite {name!r} is not configured")
        return s.model_dump()


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        out[k] = _merge(out[k], v) if isinstance(v, Mapping) and isinstance(out.get(k), dict) else v
    return out


def load_bench_config(
    path: Path, preset: str | None = None, *, per_category: int | None = None
) -> tuple[BenchConfig, list[str] | None]:
    """The config with ``[presets.<preset>]`` overlaid; also the preset's candidate list."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    preset_candidates = None
    if preset is not None:
        overlay = dict(raw.get("presets", {}).get(preset) or {})
        if not overlay:
            raise typer.BadParameter(f"no [presets.{preset}] in {path}")
        preset_candidates = overlay.pop("candidates", None)
        raw = _merge(raw, overlay)
    if per_category is not None:
        raw = _merge(raw, {"suites": {"olmocr-bench": {"per_category": per_category}}})
    return BenchConfig.model_validate(raw), preset_candidates


# --------------------------------------------------------------------------- composition


def build_suite(
    name: str, settings: Mapping[str, Any], bc: BenchConfig, *, prepare: bool = True
) -> tuple[BenchmarkSuite, dict[str, Any]]:
    """The suite adapter plus the settings that define it (stored in the run manifest)."""
    if name == "synthetic":
        s = SyntheticSettings.model_validate(settings)
        suite = SyntheticSuite(
            [(Path(d.path), d.pages) for d in s.documents],
            levels=s.levels,
            dpi=s.dpi,
            seed=s.seed,
            min_ref_chars=s.min_ref_chars,
            primary_metric=s.primary_metric,
        )
        suite.headline_exclusions = {
            e["cluster"]: e["reason"]
            for e in bc.scoring.get("synthetic", {}).get("report_separately", [])
        }
        return suite, s.model_dump()
    if name == "olmocr-bench":
        o = OlmOcrBenchSettings.model_validate(settings)
        root = Path(bc.data_dir) / "olmocr-bench"
        per_cat = o.per_category or None
        if prepare:
            subset = prepare_subset(
                root,
                revision=o.revision,
                categories=o.categories,
                per_category=per_cat,
                seed=o.seed,
                repo_id=o.repo_id,
                log=_log,
            )
        else:
            subset = root / subset_id(o.revision, o.categories, per_cat, o.seed)
        suite = OlmOcrBenchSuite(subset, long_side=o.long_side, scorer=o.scorer(), log=_log)
        return suite, o.definition()
    raise typer.BadParameter(f"unknown suite {name!r}; choose from {SUITES}")


def _suite_from_manifest(name: str, manifest: Mapping[str, Any], bc: BenchConfig):
    """Rebuild a suite exactly as the run defined it (scorer paths from the current config)."""
    stored = manifest["suites"][name].get("settings") or {}
    current = bc.suites.olmocr_bench if name == "olmocr-bench" else None
    merged = {**(current.model_dump() if current else {}), **stored}
    suite, _ = build_suite(name, merged, bc, prepare=True)
    return suite


def _reset_mlx_peak_memory() -> None:
    """mlx reports the process-wide peak: reset it so each candidate gets its own."""
    try:
        import mlx.core as mx

        mx.reset_peak_memory()
    except Exception:
        pass


def engine_factory(base: AppConfig) -> Callable[[CandidateSpec], OcrEngine]:
    def make(spec: CandidateSpec) -> OcrEngine:
        ocr = OcrConfig.model_validate(
            {
                **base.ocr.model_dump(),
                "repo_id": spec.repo_id,
                "revision": spec.revision,
                "profile": spec.profile,
                "prompt": spec.prompt,
                "max_side": spec.max_side,
                "max_tokens": spec.max_tokens,
                "dpi": spec.dpi or base.ocr.dpi,
                "temperature": spec.temperature,
                "repetition_penalty": spec.repetition_penalty,
                "served_model": None,  # the base's served name belongs to the base's model
            }
        )
        adapters = base.adapters.model_copy(update={"ocr": spec.ocr})
        cfg = base.model_copy(update={"ocr": ocr, "adapters": adapters})
        if spec.ocr == "mlx-vlm":
            _reset_mlx_peak_memory()
        return build("ocr", cfg)

    return make


# --------------------------------------------------------------------------- helpers

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="benchmark.toml")]
RunIdOpt = Annotated[str, typer.Option("--run-id", help="Results go to <runs_dir>/<run-id>/")]
SuiteOpt = Annotated[str, typer.Option("--suite", help="synthetic, olmocr-bench or all")]
PresetOpt = Annotated[str | None, typer.Option(help="Overlay the presets.<name> table, e.g. smoke")]
PerCategoryOpt = Annotated[
    int | None, typer.Option(min=0, help="olmOCR-bench PDFs per category (0 = all)")
]


def _suites(arg: str, available: list[str] | tuple[str, ...] = SUITES) -> list[str]:
    names = list(available) if arg == "all" else [s.strip() for s in arg.split(",") if s.strip()]
    if bad := [n for n in names if n not in available]:
        raise typer.BadParameter(f"unknown suites {bad}; choose from {list(available)}")
    return names


def _run_dir(bc: BenchConfig, run_id: str) -> Path:
    if not _RUN_ID.match(run_id):
        raise typer.BadParameter("run id must match [A-Za-z0-9._-]+")
    return Path(bc.runs_dir) / run_id


def _fmt_primary(s: SuiteScore) -> str:
    est = s.metrics.get(s.primary)
    if est is None or est.mean is None:
        return "–"
    pct = s.primary == "pass_rate"
    k, d = (100, 1) if pct else (1, 3)
    ci = ""
    if est.low is not None and est.high is not None:
        ci = f" [{est.low * k:.{d}f}, {est.high * k:.{d}f}]"
    return f"{est.mean * k:.{d}f}{'%' if pct else ''}{ci}"


def _print_scores(name: str, scores: Mapping[str, SuiteScore]) -> None:
    if not scores:
        return
    primary = next(iter(scores.values())).primary
    table = Table(title=f"{name}: {primary}")
    for col in ("candidate", primary, "outputs", "issues"):
        table.add_column(col)
    for c in rank(scores):
        s = scores[c]
        issues = "; ".join(s.errors + s.details.get("warnings", []))
        table.add_row(c, _fmt_primary(s), f"{s.n_outputs}/{s.n_samples}", escape(issues))
    console.print(table)


# --------------------------------------------------------------------------- commands


@bench_app.command()
def prepare(
    suite: SuiteOpt = "all",
    preset: PresetOpt = None,
    per_category: PerCategoryOpt = None,
    config: ConfigOpt = DEFAULT_BENCH_CONFIG,
) -> None:
    """Download / build suite data and check that samples load."""
    bc, _ = load_bench_config(config, preset, per_category=per_category)
    for name in _suites(suite):
        s, _ = build_suite(name, bc.suite_settings(name), bc, prepare=True)
        samples = s.samples()
        counts: dict[str, int] = {}
        for x in samples:
            counts[x.category] = counts.get(x.category, 0) + 1
        breakdown = ", ".join(f"{k} {v}" for k, v in counts.items())
        console.print(f"[bold]{name}[/]: {len(samples)} samples ({escape(breakdown)})")
        console.print(f"  fingerprint {s.fingerprint[:12]}")
        for msg in getattr(s, "skipped", []):
            console.print(f"  skipped {escape(msg)}")
        if samples:
            img = samples[0].load_image()
            console.print(f"  first sample {escape(samples[0].id)} renders at {img.size}")


@bench_app.command()
def run(  # keyword-only: typer passes options by name
    *,
    run_id: RunIdOpt,
    suite: SuiteOpt = "all",
    candidates: Annotated[
        str | None, typer.Option(help="Comma-separated names or 'all' (default: preset or all)")
    ] = None,
    preset: PresetOpt = None,
    per_category: PerCategoryOpt = None,
    time_budget: Annotated[
        float | None, typer.Option(min=0, help="Max seconds per candidate per suite")
    ] = None,
    retry_errors: Annotated[bool, typer.Option(help="Re-run samples that failed")] = False,
    config: ConfigOpt = DEFAULT_BENCH_CONFIG,
) -> None:
    """Transcribe every suite sample with every candidate (resumable: rerun to continue)."""
    bc, preset_candidates = load_bench_config(config, preset, per_category=per_category)
    specs = bc.specs()
    wanted = candidates or (",".join(preset_candidates) if preset_candidates else "all")
    names = list(specs) if wanted == "all" else [c.strip() for c in wanted.split(",")]
    if bad := [n for n in names if n not in specs]:
        raise typer.BadParameter(f"unknown candidates {bad}; configured: {list(specs)}")
    run_dir = _run_dir(bc, run_id)
    base = load_config(Path(bc.pipeline_config) if bc.pipeline_config else None)
    runner = BenchmarkRunner(engine_factory(base), log=_log)
    table = Table(title=f"run {run_id}")
    for col in ("suite", "candidate", "done", "skipped", "errors", "left", "seconds", "stopped"):
        table.add_column(col)
    for name in _suites(suite):
        s, settings = build_suite(name, bc.suite_settings(name), bc, prepare=True)
        console.rule(f"[bold]{name}[/] ({len(s.samples())} samples)")
        try:
            results = runner.run(
                s,
                [specs[n] for n in names],
                run_dir,
                time_budget_s=time_budget,
                retry_errors=retry_errors,
                settings=settings,
            )
        except RunMismatchError as e:
            console.print(f"[red]error[/]: {escape(str(e))}")
            raise typer.Exit(2) from e
        for r in results:
            table.add_row(
                name,
                r.candidate,
                str(r.done),
                str(r.skipped),
                str(r.errors),
                str(r.remaining),
                f"{r.seconds:.0f}",
                escape(r.stopped or ""),
            )
    console.print(table)
    console.print(f"outputs -> {escape(str(run_dir))}")


def _sync_telemetry(run_dir: Path, suite: str, current: dict[str, dict[str, str]]) -> set[str]:
    """Keep telemetry's chars / empty true to the output files whenever they drifted (also
    for files an earlier version of refresh_outputs rewrote without updating telemetry)."""
    drifted: set[str] = set()
    for cand, texts in current.items():
        tel = run_dir / "telemetry" / suite / f"{cand}.jsonl"
        records = [json.loads(line) for line in tel.read_text().splitlines() if line.strip()]
        drift = False
        for r in records:
            t = texts.get(r.get("sample_id"))
            if t is not None and (r.get("chars") != len(t) or r.get("empty") != (not t.strip())):
                r["chars"], r["empty"], drift = len(t), not t.strip(), True
        if drift:
            tmp = tel.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in records))
            tmp.replace(tel)
            drifted.add(cand)  # its throughput numbers changed: re-score / re-report it
    return drifted


def refresh_outputs(
    run_dir: Path, suite: BenchmarkSuite, manifest: dict[str, Any], names: list[str] | None = None
) -> set[str]:
    """Re-apply each candidate's *current* profile clean-up to its stored outputs.

    Model outputs are kept as generated; this only re-runs our post-processing, so a
    clean-up bug fixed after a run (e.g. olmOCR front matter inside an unclosed code
    fence) does not have to be paid for with a re-transcription. Originals are backed up
    once under raw_outputs/, every rewrite is listed in postprocess_log.json, and
    candidates still being transcribed are left alone. Returns the changed candidates.
    """
    from ..adapters.ocr.profiles import profile_for

    samples = suite.samples()
    log_path = run_dir / "postprocess_log.json"
    log: dict[str, Any] = json.loads(log_path.read_text()) if log_path.exists() else {}
    changed: set[str] = set()
    current: dict[str, dict[str, str]] = defaultdict(dict)  # candidate -> sample id -> text
    for cand in manifest["suites"][suite.name]["candidates"]:
        if names and cand not in names:
            continue
        tel = run_dir / "telemetry" / suite.name / f"{cand}.jsonl"
        if not tel.exists() or sum(1 for _ in tel.open()) < len(samples):
            continue  # still transcribing (or never ran): don't touch files being written
        post = profile_for(manifest["candidates"][cand]["profile"]).postprocess
        for s in samples:
            p = suite.output_path(run_dir, cand, s)
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            new = post(text)
            current[cand][s.id] = new
            if new == text:
                continue
            backup = run_dir / "raw_outputs" / p.relative_to(run_dir)
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(text, encoding="utf-8")
            p.write_text(new, encoding="utf-8")
            changed.add(cand)
            rel = str(p.relative_to(run_dir))
            entries = log.setdefault(suite.name, {}).setdefault(cand, [])
            if rel not in entries:
                entries.append(rel)
    changed |= _sync_telemetry(run_dir, suite.name, current)
    if changed:
        log_path.write_text(json.dumps(log, indent=2))
        console.print(f"[yellow]re-applied clean-up[/] to {escape(', '.join(sorted(changed)))}")
    return changed


@bench_app.command()
def score(
    run_id: RunIdOpt,
    suite: SuiteOpt = "all",
    candidates: Annotated[str | None, typer.Option(help="Comma-separated (default: all)")] = None,
    config: ConfigOpt = DEFAULT_BENCH_CONFIG,
) -> None:
    """Score a run's outputs (olmOCR-bench: the official scorer) into scores/<suite>.json."""
    bc, _ = load_bench_config(config)
    run_dir = _run_dir(bc, run_id)
    manifest = read_manifest(run_dir)
    names = [c.strip() for c in candidates.split(",")] if candidates else None
    for name in _suites(suite, tuple(manifest["suites"])):
        s = _suite_from_manifest(name, manifest, bc)
        refresh_outputs(run_dir, s, manifest, names)
        _print_scores(name, score_run(s, run_dir, names))


@bench_app.command()
def report(
    run_id: RunIdOpt,
    rescore: Annotated[bool, typer.Option(help="Score again even if scores exist")] = False,
    resamples: Annotated[int, typer.Option(min=100, help="Paired bootstrap resamples")] = 10_000,
    seed: int = 0,
    config: ConfigOpt = DEFAULT_BENCH_CONFIG,
) -> None:
    """Write <run>/summary.json and <run>/report.md (scores new, changed, incomplete or
    failed candidates first, and scores made under older scoring rules)."""
    bc, _ = load_bench_config(config)
    run_dir = _run_dir(bc, run_id)
    manifest = read_manifest(run_dir)
    for name, entry in manifest["suites"].items():
        version = SCORING_VERSIONS.get(name)
        s = _suite_from_manifest(name, manifest, bc)
        refreshed = refresh_outputs(run_dir, s, manifest)
        todo = (
            entry["candidates"]
            if rescore
            else sorted(set(needs_scoring(run_dir, name, scoring_version=version)) | refreshed)
        )
        if todo:
            score_run(s, run_dir, todo)
        _print_scores(name, load_scores(run_dir, name))
    json_path, md_path = write_report(run_dir, n_boot=resamples, seed=seed)
    console.print(f"summary -> {escape(str(json_path))}\nreport  -> {escape(str(md_path))}")


@bench_app.command("candidates")
def list_candidates(config: ConfigOpt = DEFAULT_BENCH_CONFIG) -> None:
    """List the configured candidates."""
    bc, _ = load_bench_config(config)
    table = Table(title="OCR benchmark candidates")
    for col in ("name", "model", "revision", "profile", "adapter"):
        table.add_column(col)
    for spec in bc.specs().values():
        table.add_row(spec.name, spec.repo_id, spec.revision[:10], spec.profile, spec.ocr)
    console.print(table)
