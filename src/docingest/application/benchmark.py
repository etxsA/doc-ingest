"""Use case: benchmark OCR candidates on suites, resumably, and report with CIs.

A run directory (``data/bench/runs/<run-id>/``) is self-describing::

    manifest.json                     suites (fingerprint + settings), candidate specs,
                                      library versions, machine
    <suite outputs>                   wherever ``suite.output_path`` puts them
    telemetry/<suite>/<cand>.jsonl    one JSON line per transcription attempt
    scores/<suite>.json               ``SuiteScore`` per candidate (``score_run``)
    summary.json, report.md           (``write_report``)

Resumable: a sample whose output file exists is skipped, so an interrupted run picks
up where it stopped. A failed transcription is recorded in the telemetry and written
as an empty output, so it scores as a miss (official scorers need one file per page);
``retry_errors`` re-runs those samples. A streak of failures stops the candidate and
removes the streak's outputs instead: it points at the model, not at those pages.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ..ports import BenchmarkSuite, CandidateSpec, OcrEngine, Sample, SuiteScore
from .ingest import PIPELINE_VERSION
from .stats import PairedResult, paired_bootstrap, quantile

Log = Callable[[str], None]
EngineFactory = Callable[[CandidateSpec], OcrEngine]

MANIFEST = "manifest.json"
SCORES_DIR = "scores"
TELEMETRY_DIR = "telemetry"
# Library versions recorded in the manifest (absent ones are recorded as None).
TRACKED_DISTRIBUTIONS = (
    "docingest",
    "mlx-vlm",
    "mlx",
    "transformers",
    "pypdfium2",
    "pillow",
    "numpy",
    "jiwer",
    "huggingface-hub",
)


class RunMismatchError(ValueError):
    """The run directory was started with a different suite or candidate definition."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def _write_atomic(path: Path, text: str) -> None:
    """Write-then-rename: a crash never leaves a truncated file that resume would skip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _num(x: float | None, digits: int = 4) -> float | None:
    return None if x is None or math.isnan(x) else round(x, digits)


# --------------------------------------------------------------------------- provenance


def _sysctl(key: str) -> str | None:
    try:
        out = subprocess.run(
            ["sysctl", "-n", key], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def machine_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": platform.processor() or None,
        "ram_gb": None,
    }
    if sys.platform == "darwin":
        info["cpu"] = _sysctl("machdep.cpu.brand_string") or info["cpu"]
        mem = _sysctl("hw.memsize")
        info["ram_gb"] = round(int(mem) / 2**30, 1) if mem and mem.isdigit() else None
    else:
        with contextlib.suppress(ValueError, OSError, AttributeError):
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            info["ram_gb"] = round(ram / 2**30, 1)
    return info


def library_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {
        "python": platform.python_version(),
        "pipeline": PIPELINE_VERSION,
    }
    for dist in TRACKED_DISTRIBUTIONS:
        try:
            versions[dist] = version(dist)
        except PackageNotFoundError:
            versions[dist] = None
    return versions


# --------------------------------------------------------------------------- telemetry


def telemetry_path(run_dir: Path, suite: str, candidate: str) -> Path:
    return run_dir / TELEMETRY_DIR / suite / f"{candidate}.jsonl"


def read_telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def latest_by_sample(records: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """The last attempt per sample (a resumed / retried run appends new attempts)."""
    return {r["sample_id"]: r for r in records}


def throughput(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Speed and failure-mode statistics over the latest attempt of each sample."""
    recs = list(latest_by_sample(records).values())
    ok = [r for r in recs if not r.get("error")]
    secs = [r["seconds"] for r in ok if r.get("seconds") is not None]
    timed = [r for r in ok if r.get("seconds") and r.get("gen_tokens") is not None]
    tok_time = sum(r["seconds"] for r in timed)
    peaks = [r["peak_memory_gb"] for r in ok if r.get("peak_memory_gb") is not None]
    return {
        "pages": len(recs),
        "ok": len(ok),
        "errors": len(recs) - len(ok),
        "median_s": _num(quantile(secs, 0.5), 3),
        "p90_s": _num(quantile(secs, 0.9), 3),
        "total_s": round(sum(secs), 1),
        "gen_tok_s": round(sum(r["gen_tokens"] for r in timed) / tok_time, 1) if tok_time else None,
        "peak_memory_gb": round(max(peaks), 2) if peaks else None,
        "truncation_rate": _num(
            sum(r.get("finish_reason") == "length" for r in ok) / len(ok) if ok else math.nan
        ),
        "empty_rate": _num(sum(bool(r.get("empty")) for r in ok) / len(ok) if ok else math.nan),
    }


# --------------------------------------------------------------------------- runner


@dataclass
class CandidateRun:
    """What one ``run`` call did for one candidate on one suite."""

    suite: str
    candidate: str
    total: int
    skipped: int = 0  # output already present (resumed run)
    done: int = 0
    errors: int = 0
    seconds: float = 0.0
    stopped: str | None = None  # why the candidate stopped before the end

    @property
    def remaining(self) -> int:
        return self.total - self.skipped - self.done - self.errors


class BenchmarkRunner:
    """Transcribes every sample of a suite with each candidate engine, one model at a time."""

    def __init__(
        self,
        engine_factory: EngineFactory,
        log: Log = print,
        *,
        max_consecutive_errors: int = 5,
        clock: Callable[[], float] = time.perf_counter,
        environment: Callable[[], dict[str, Any]] = machine_info,
    ):
        self.engine_factory = engine_factory
        self.log = log
        self.max_consecutive_errors = max_consecutive_errors
        self.clock = clock
        self.environment = environment

    def run(
        self,
        suite: BenchmarkSuite,
        candidates: Sequence[CandidateSpec],
        run_dir: Path,
        *,
        time_budget_s: float | None = None,
        retry_errors: bool = False,
        settings: Mapping[str, Any] | None = None,
    ) -> list[CandidateRun]:
        """Run ``candidates`` on ``suite``. ``time_budget_s`` caps each candidate's wall time;
        ``settings`` (the suite's resolved config) is stored so the run can be re-scored."""
        samples = suite.samples()
        ids = [s.id for s in samples]
        if len(set(ids)) != len(ids):
            raise ValueError(f"suite {suite.name!r} has duplicate sample ids")
        self._record(run_dir, suite, candidates, settings, len(samples))
        return [
            self._run_candidate(
                suite,
                samples,
                spec,
                run_dir,
                time_budget_s=time_budget_s,
                retry_errors=retry_errors,
            )
            for spec in candidates
        ]

    def _run_candidate(
        self,
        suite: BenchmarkSuite,
        samples: list[Sample],
        spec: CandidateSpec,
        run_dir: Path,
        *,
        time_budget_s: float | None,
        retry_errors: bool,
    ) -> CandidateRun:
        tele = telemetry_path(run_dir, suite.name, spec.name)
        failed: set[str] = set()
        if retry_errors:
            last = latest_by_sample(read_telemetry(tele))
            failed = {k for k, r in last.items() if r.get("error")}
        todo = [
            s
            for s in samples
            if s.id in failed or not suite.output_path(run_dir, spec.name, s).exists()
        ]
        stats = CandidateRun(suite.name, spec.name, len(samples), skipped=len(samples) - len(todo))
        if not todo:
            self.log(f"[{suite.name}] {spec.name}: all {len(samples)} outputs present, skipping")
            return stats
        self.log(f"[{suite.name}] {spec.name}: {len(todo)}/{len(samples)} samples to transcribe")
        engine = self.engine_factory(spec)
        start = self.clock()
        streak: list[Sample] = []  # consecutive failures, most likely systemic
        tele.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tele.open("a", encoding="utf-8") as out:
                for i, sample in enumerate(todo, 1):
                    if time_budget_s is not None and self.clock() - start >= time_budget_s:
                        stats.stopped = f"time budget of {time_budget_s:g}s reached"
                        break
                    rec = self._transcribe(engine, suite, sample, run_dir, spec.name)
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    if rec["error"]:
                        stats.errors += 1
                        streak.append(sample)
                        self.log(f"  [{i}/{len(todo)}] {sample.id}: ERROR {rec['error']}")
                    else:
                        stats.done += 1
                        streak.clear()
                        self.log(
                            f"  [{i}/{len(todo)}] {sample.id}: {rec['seconds']:.1f}s "
                            f"{rec['gen_tokens']} tok {rec['finish_reason']} {rec['chars']} chars"
                        )
                    if len(streak) >= self.max_consecutive_errors:
                        # A streak (model failed to load, out of memory) says nothing about
                        # those pages: drop their empty outputs so a resume retries them.
                        for failed_sample in streak:
                            suite.output_path(run_dir, spec.name, failed_sample).unlink()
                        stats.stopped = f"{len(streak)} consecutive errors"
                        break
        finally:
            stats.seconds = round(self.clock() - start, 2)
            unload = getattr(engine, "unload", None)
            if callable(unload):
                unload()  # free the weights before the next model loads
        if stats.stopped:
            self.log(
                f"[{suite.name}] {spec.name}: stopped ({stats.stopped}), "
                f"{stats.remaining} samples left; rerun the same command to resume"
            )
        return stats

    def _transcribe(
        self, engine: OcrEngine, suite: BenchmarkSuite, sample: Sample, run_dir: Path, name: str
    ) -> dict[str, Any]:
        t0 = self.clock()
        res, error = None, None
        try:
            res = engine.transcribe(sample.load_image())
        except Exception as e:  # one bad page must not stop a multi-hour benchmark
            error = f"{type(e).__name__}: {e}"
        text = res.text if res is not None else ""
        _write_atomic(suite.output_path(run_dir, name, sample), text)
        wall = self.clock() - t0
        return {
            "sample_id": sample.id,
            "category": sample.category,
            "seconds": round(res.seconds if res is not None else wall, 3),
            "wall_seconds": round(wall, 3),
            "gen_tokens": res.gen_tokens if res is not None else None,
            "finish_reason": res.finish_reason if res is not None else None,
            "peak_memory_gb": res.peak_memory_gb if res is not None else None,
            "chars": len(text),
            "empty": not text.strip(),
            "error": error,
            "ts": _now(),
        }

    def _record(
        self,
        run_dir: Path,
        suite: BenchmarkSuite,
        candidates: Sequence[CandidateSpec],
        settings: Mapping[str, Any] | None,
        n_samples: int,
    ) -> None:
        """Create or extend manifest.json; refuse to mix definitions in one run."""
        path = run_dir / MANIFEST
        m = _read_json(path) or {"created_at": _now(), "suites": {}, "candidates": {}}
        entry = m["suites"].get(suite.name)
        if entry and entry["fingerprint"] != suite.fingerprint:
            raise RunMismatchError(
                f"run {run_dir.name!r} already has suite {suite.name!r} with different data or "
                f"settings (fingerprint {entry['fingerprint'][:12]} != {suite.fingerprint[:12]}); "
                "use the same settings or a new run id"
            )
        for spec in candidates:
            old = m["candidates"].get(spec.name)
            if old is not None and old != spec.to_dict():
                raise RunMismatchError(
                    f"run {run_dir.name!r} already has candidate {spec.name!r} with a different "
                    "spec; rename the candidate or use a new run id"
                )
        versions, machine = library_versions(), self.environment()
        for key, now in (("versions", versions), ("machine", machine)):
            if key not in m:
                m[key] = now
            elif m[key] != now:  # keep the original, record what changed mid-run (once)
                changes = m.setdefault("changes", [])
                if not any(c.get(key) == now for c in changes):
                    changes.append({"at": _now(), key: now})
                self.log(f"warning: {key} differ from when run {run_dir.name!r} started")
        names = sorted({*(entry or {}).get("candidates", []), *(c.name for c in candidates)})
        m["suites"][suite.name] = {
            "fingerprint": suite.fingerprint,
            "settings": dict(settings) if settings is not None else (entry or {}).get("settings"),
            "n_samples": n_samples,
            "candidates": names,
        }
        m["candidates"].update({c.name: c.to_dict() for c in candidates})
        m["updated_at"] = _now()
        _write_atomic(path, json.dumps(m, indent=2))


def read_manifest(run_dir: Path) -> dict[str, Any]:
    m = _read_json(run_dir / MANIFEST)
    if m is None:
        raise FileNotFoundError(f"no benchmark run at {run_dir} (missing {MANIFEST})")
    return m


# --------------------------------------------------------------------------- scoring


def score_run(
    suite: BenchmarkSuite, run_dir: Path, candidates: Sequence[str] | None = None
) -> dict[str, SuiteScore]:
    """Score candidates (default: all recorded for the suite) and merge into scores/<suite>.json."""
    entry = read_manifest(run_dir)["suites"].get(suite.name)
    if entry is None:
        raise ValueError(f"suite {suite.name!r} was not run in {run_dir}")
    if entry["fingerprint"] != suite.fingerprint:
        raise RunMismatchError(f"suite {suite.name!r} changed since run {run_dir.name!r}")
    names = list(candidates) if candidates else entry["candidates"]
    scores = suite.score(run_dir, names)
    path = run_dir / SCORES_DIR / f"{suite.name}.json"
    merged = _read_json(path) or {}
    merged.update({name: s.to_dict() for name, s in scores.items()})
    _write_atomic(path, json.dumps(merged, indent=1))
    return scores


def needs_scoring(run_dir: Path, suite: str) -> list[str]:
    """Candidates of ``suite`` without scores, or transcribed again since they were scored."""
    entry = read_manifest(run_dir)["suites"][suite]
    path = run_dir / SCORES_DIR / f"{suite}.json"
    scored = _read_json(path) or {}
    stamp = path.stat().st_mtime if path.exists() else 0.0
    out = []
    for c in entry["candidates"]:
        tele = telemetry_path(run_dir, suite, c)
        if c not in scored or (tele.exists() and tele.stat().st_mtime > stamp):
            out.append(c)
    return out


def load_scores(run_dir: Path, suite: str) -> dict[str, SuiteScore]:
    raw = _read_json(run_dir / SCORES_DIR / f"{suite}.json") or {}
    return {name: SuiteScore.from_dict(d) for name, d in raw.items()}


def _primary_mean(s: SuiteScore) -> float | None:
    est = s.metrics.get(s.primary)
    return est.mean if est is not None else None


def rank(scores: Mapping[str, SuiteScore]) -> list[str]:
    """Best first on the primary metric. Candidates with scoring errors (incomplete runs)
    rank after complete ones; candidates without any score come last."""

    def key(name: str) -> tuple[int, float, str]:
        s = scores[name]
        mean = _primary_mean(s)
        if mean is None:
            return (2, math.inf, name)
        return (int(bool(s.errors)), -mean if s.higher_is_better else mean, name)

    return sorted(scores, key=key)


def compare(a: SuiteScore, b: SuiteScore, *, n: int = 10_000, seed: int = 0) -> PairedResult:
    """Paired bootstrap of a - b on the primary metric over the units both scored."""
    metric = a.primary
    common = sorted(set(a.units) & set(b.units))
    xs = [a.units[u].get(metric) for u in common]
    ys = [b.units[u].get(metric) for u in common]
    groups = [a.unit_groups.get(u, "") for u in common] if a.unit_groups else None
    return paired_bootstrap(xs, ys, groups=groups, n=n, seed=seed)


# --------------------------------------------------------------------------- report


def build_summary(run_dir: Path, *, n_boot: int = 10_000, seed: int = 0) -> dict[str, Any]:
    manifest = read_manifest(run_dir)
    suites: dict[str, Any] = {}
    for name, entry in manifest["suites"].items():
        scores = load_scores(run_dir, name)
        ranking = rank(scores)
        best = ranking[0] if ranking and _primary_mean(scores[ranking[0]]) is not None else None
        paired = {
            c: {
                k: None if isinstance(v, float) and math.isnan(v) else v
                for k, v in asdict(compare(scores[c], scores[best], n=n_boot, seed=seed)).items()
            }
            for c in ranking
            if best is not None and c != best and _primary_mean(scores[c]) is not None
        }
        tp = {
            c: throughput(read_telemetry(telemetry_path(run_dir, name, c)))
            for c in entry["candidates"]
        }
        compact = {}
        for c, s in scores.items():
            d = s.to_dict()
            d.pop("units")  # per-unit scores stay in scores/<suite>.json
            d.pop("unit_groups")
            compact[c] = d
        suites[name] = {
            "fingerprint": entry["fingerprint"],
            "n_samples": entry["n_samples"],
            "candidates": entry["candidates"],
            "unscored": sorted(set(entry["candidates"]) - set(scores)),
            "ranking": ranking,
            "best": best,
            "scores": compact,
            "paired_vs_best": paired,
            "throughput": tp,
        }
    return {
        "run_id": run_dir.name,
        "generated_at": _now(),
        "bootstrap": {"paired_resamples": n_boot, "seed": seed},
        "manifest": manifest,
        "suites": suites,
    }


def _fmt(est: Mapping[str, Any] | None, pct: bool, *, signed: bool = False) -> str:
    if not est or est.get("mean") is None:
        return "–"
    scale, unit, digits = (100.0, "%", 1) if pct else (1.0, "", 3)

    def f(x: float, sign: str = "") -> str:
        return f"{x * scale:{sign}.{digits}f}"

    out = f(est["mean"], "+" if signed else "") + unit
    if est.get("low") is not None and est.get("high") is not None:
        out += f" [{f(est['low'])}, {f(est['high'])}]"
    return out


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    def cell(v: Any) -> str:
        return "–" if v is None else str(v).replace("|", "\\|")

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return lines


def _pct(x: float | None) -> str | None:
    return None if x is None else f"{x * 100:.1f}%"


def _pvalue(p: float | None, n_boot: int) -> str | None:
    if p is None:
        return None
    return f"<{1 / n_boot:g}" if p == 0 else f"{p:.4f}"


def render_report(summary: Mapping[str, Any]) -> str:
    m = summary["manifest"]
    n_boot = summary["bootstrap"]["paired_resamples"]
    v, mach = m.get("versions", {}), m.get("machine", {})
    out = [
        f"# OCR benchmark: run `{summary['run_id']}`",
        "",
        f"Generated {summary['generated_at']} · docingest {v.get('docingest')} "
        f"(pipeline {v.get('pipeline')}) · mlx-vlm {v.get('mlx-vlm')} · "
        f"{mach.get('cpu')}, {mach.get('ram_gb')} GB RAM · {mach.get('platform')}",
        "",
        "Intervals are 95% bootstrap CIs. Paired comparisons resample the units both "
        f"candidates scored ({summary['bootstrap']['paired_resamples']} resamples, "
        f"seed {summary['bootstrap']['seed']}).",
        "",
        "## Candidates",
        "",
    ]
    out += _table(
        ["candidate", "model", "revision", "profile", "adapter", "max_side", "max_tokens"],
        (
            [
                n,
                c["repo_id"],
                c["revision"][:10],
                c["profile"],
                c["ocr"],
                c["max_side"],
                c["max_tokens"],
            ]
            for n, c in sorted(m["candidates"].items())
        ),
    )
    for name, s in summary["suites"].items():
        out += ["", f"## Suite `{name}`", ""]
        out.append(f"Fingerprint `{s['fingerprint'][:12]}`, {s['n_samples']} samples.")
        scores = s["scores"]
        if not scores:
            out += ["", "_Not scored yet: run `docingest bench score`._"]
        else:
            any_s = next(iter(scores.values()))
            primary, hib = any_s["primary"], any_s["higher_is_better"]
            pct = primary == "pass_rate"
            metrics = list(any_s["metrics"])
            direction = "higher" if hib else "lower"
            out += ["", f"Primary metric: **{primary}** ({direction} is better).", ""]
            out += ["### Overall", ""]
            out += _table(
                ["rank", "candidate", *metrics, "outputs"],
                (
                    [
                        i,
                        c,
                        *(_fmt(scores[c]["metrics"].get(k), pct) for k in metrics),
                        f"{scores[c]['n_outputs']}/{scores[c]['n_samples']}",
                    ]
                    for i, c in enumerate(s["ranking"], 1)
                ),
            )
            cats = sorted({k for c in scores.values() for k in c["by_category"]})
            if cats:
                out += ["", f"### By category ({primary})", ""]
                out += _table(
                    ["candidate", *cats],
                    (
                        [
                            c,
                            *(
                                _fmt(scores[c]["by_category"].get(k, {}).get(primary), pct)
                                for k in cats
                            ),
                        ]
                        for c in s["ranking"]
                    ),
                )
            if s["paired_vs_best"]:
                out += ["", f"### Paired comparison vs best (`{s['best']}`)", ""]
                rows = []
                for c, p in s["paired_vs_best"].items():
                    ci = {"mean": p["diff"], "low": p["low"], "high": p["high"]}
                    if p["low"] is None or p["high"] is None:
                        sig = None
                    else:
                        sig = "yes" if not p["low"] <= 0 <= p["high"] else "no"
                    rows.append(
                        [c, _fmt(ci, pct, signed=True), _pvalue(p["p_value"], n_boot), sig, p["n"]]
                    )
                out += _table(
                    [
                        "candidate",
                        f"Δ {primary} (candidate − best) [CI]",
                        "p",
                        "significant",
                        "pairs",
                    ],
                    rows,
                )
        out += ["", "### Throughput and failure modes", ""]
        out += _table(
            [
                "candidate",
                "pages",
                "median s/page",
                "p90 s/page",
                "gen tok/s",
                "peak GB",
                "truncated",
                "empty",
                "errors",
            ],
            (
                [
                    c,
                    t["pages"],
                    t["median_s"],
                    t["p90_s"],
                    t["gen_tok_s"],
                    t["peak_memory_gb"],
                    _pct(t["truncation_rate"]),
                    _pct(t["empty_rate"]),
                    t["errors"],
                ]
                for c, t in s["throughput"].items()
            ),
        )
        issues = [f"`{c}`: {e}" for c, sc in scores.items() for e in sc["errors"]]
        issues += [f"`{c}`: not scored" for c in s["unscored"]]
        if issues:
            out += ["", "### Issues", "", *(f"- {i}" for i in issues)]
    return "\n".join(out) + "\n"


def write_report(run_dir: Path, *, n_boot: int = 10_000, seed: int = 0) -> tuple[Path, Path]:
    summary = build_summary(run_dir, n_boot=n_boot, seed=seed)
    json_path, md_path = run_dir / "summary.json", run_dir / "report.md"
    _write_atomic(json_path, json.dumps(summary, indent=2))
    _write_atomic(md_path, render_report(summary))
    return json_path, md_path
