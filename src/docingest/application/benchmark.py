"""Use case: benchmark OCR candidates on suites, resumably, and report with CIs.

A run directory (``data/bench/runs/<run-id>/``) is self-describing::

    manifest.json                     suites (fingerprint + settings), candidate specs,
                                      library versions, machine
    <suite outputs>                   wherever ``suite.output_path`` puts them
    telemetry/<suite>/<cand>.jsonl    one JSON line per transcription (with the engine's
                                      retry ladder: attempts, first finish, all tokens)
    scores/<suite>.json               ``SuiteScore`` per candidate (``score_run``), stamped
                                      with the telemetry it saw, the scoring version and
                                      the suite's scoring options
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
from .stats import MIN_CLUSTERS, PairedResult, paired_bootstrap, quantile

Log = Callable[[str], None]
EngineFactory = Callable[[CandidateSpec], OcrEngine]

MANIFEST = "manifest.json"
SCORES_DIR = "scores"
TELEMETRY_DIR = "telemetry"
RAW_DIR = "model_raw"  # raw model output per sample, when clean-up changed it
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


def _rate(hits: int, total: int) -> float | None:
    return _num(hits / total) if total else None


def _gen_speed(ok: Sequence[Mapping[str, Any]]) -> tuple[float | None, str | None]:
    """Generated tokens per second and what the time measured.

    ``decode``: every attempt's tokens over its decode time (the engine measured it).
    ``end-to-end``: every attempt's tokens over page time, prefill included (HTTP
    engines). ``legacy``: telemetry from before retries were recorded, i.e. the final
    attempt's tokens over the time of all attempts; a retried page makes it far too low.
    The last two are lower bounds on decode speed.
    """
    if not ok:
        return None, None
    if all(r.get("gen_seconds") is not None for r in ok):
        secs = sum(r["gen_seconds"] for r in ok)
        tokens = sum(r.get("total_gen_tokens") or 0 for r in ok)
        return (round(tokens / secs, 1) if secs else None), "decode"
    timed = [r for r in ok if r.get("seconds") and r.get("gen_tokens") is not None]
    secs = sum(r["seconds"] for r in timed)
    if not secs:
        return None, None
    tokens = sum(r.get("total_gen_tokens") or r["gen_tokens"] for r in timed)
    basis = "legacy" if any("attempts" not in r for r in timed) else "end-to-end"
    return round(tokens / secs, 1), basis


def throughput(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Speed and failure-mode statistics over the latest record of each sample.

    Truncation is reported twice: ``first_truncation_rate`` (the first attempt hit
    max_tokens, i.e. the model looped, even if a retry then finished) and
    ``truncation_rate`` (the scored text is still cut off). Records written before the
    retry ladder was recorded carry no ``attempts``: for them only final truncations are
    visible, so ``first_truncation_rate`` and ``retried_rate`` become lower bounds
    (``lower_bound``: True; ``retried_rate`` None when no record says).
    """
    recs = list(latest_by_sample(records).values())
    ok = [r for r in recs if not r.get("error")]
    secs = [r["seconds"] for r in ok if r.get("seconds") is not None]
    peaks = [r["peak_memory_gb"] for r in ok if r.get("peak_memory_gb") is not None]
    known = [r for r in ok if "attempts" in r]  # the rest: written by an older runner
    first = sum(r.get("first_finish_reason", r.get("finish_reason")) == "length" for r in ok)
    speed, basis = _gen_speed(ok)
    return {
        "pages": len(recs),
        "ok": len(ok),
        "errors": len(recs) - len(ok),
        "median_s": _num(quantile(secs, 0.5), 3),
        "p90_s": _num(quantile(secs, 0.9), 3),
        "total_s": round(sum(secs), 1),
        "gen_tok_s": speed,
        "gen_tok_s_basis": basis,
        "peak_memory_gb": round(max(peaks), 2) if peaks else None,
        "first_truncation_rate": _rate(first, len(ok)),
        "truncation_rate": _rate(sum(r.get("finish_reason") == "length" for r in ok), len(ok)),
        "retried_rate": _rate(sum((r.get("attempts") or 1) > 1 for r in known), len(ok))
        if known
        else None,
        "lower_bound": len(known) < len(ok),
        "empty_rate": _rate(sum(bool(r.get("empty")) for r in ok), len(ok)),
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
        raw = res.raw_text if res is not None else None
        if raw is not None and raw != text:  # the model's own words, before our clean-up
            _write_atomic(run_dir / RAW_DIR / suite.name / name / f"{sample.id}.txt", raw)
        wall = self.clock() - t0
        return {
            "sample_id": sample.id,
            "category": sample.category,
            "seconds": round(res.seconds if res is not None else wall, 3),
            "wall_seconds": round(wall, 3),
            "gen_tokens": res.gen_tokens if res is not None else None,
            "finish_reason": res.finish_reason if res is not None else None,
            # The engine's retry ladder: a first attempt that hit max_tokens stays visible
            # even when a retry finished, and tok/s counts every generated token.
            "attempts": res.attempts if res is not None else None,
            "first_finish_reason": res.first_finish_reason if res is not None else None,
            "total_gen_tokens": res.total_gen_tokens if res is not None else None,
            "gen_seconds": _num(res.gen_seconds, 3) if res is not None else None,
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


def scoring_options(suite: BenchmarkSuite) -> dict[str, Any]:
    """The suite's scoring-time options (its optional ``scoring_options``; default none)."""
    return dict(getattr(suite, "scoring_options", None) or {})


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
    # Stamped *before* scoring: a transcription landing while the scorer runs makes the
    # stamp stale, so the next report scores the candidate again.
    stamps = {c: telemetry_stamp(run_dir, suite.name, c) for c in names}
    options = scoring_options(suite)
    scores = suite.score(run_dir, names)
    for name, s in scores.items():
        s.stamp = {"telemetry": stamps.get(name), "scoring_options": options}
    path = run_dir / SCORES_DIR / f"{suite.name}.json"
    merged = _read_json(path) or {}
    merged.update({name: s.to_dict() for name, s in scores.items()})
    _write_atomic(path, json.dumps(merged, indent=1))
    return scores


def telemetry_stamp(run_dir: Path, suite: str, candidate: str) -> dict[str, int] | None:
    """Size, mtime and record count of a candidate's telemetry (None: never transcribed)."""
    path = telemetry_path(run_dir, suite, candidate)
    try:
        st = path.stat()
        lines = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except FileNotFoundError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "records": lines}


def _stale(
    saved: Mapping[str, Any],
    current: dict[str, int] | None,
    version: int | None,
    options: Mapping[str, Any] | None,
) -> bool:
    """A saved score that does not describe the candidate's current outputs."""
    if saved.get("errors") or saved.get("n_outputs", 0) < saved.get("n_samples", 0):
        return True  # partial or failed (scorer missing, crashed): the cause may be gone now
    if (saved.get("metrics", {}).get(saved.get("primary"), {}) or {}).get("mean") is None:
        return True
    if version is not None and saved.get("scoring_version") != version:
        return True  # scored under older rules
    stamp = saved.get("stamp") or {}
    if options is not None and stamp.get("scoring_options", {}) != options:
        return True  # scored with other scoring options (e.g. [scoring.synthetic])
    return "telemetry" not in stamp or stamp["telemetry"] != current


def needs_scoring(
    run_dir: Path,
    suite: str,
    *,
    scoring_version: int | None = None,
    scoring_options: Mapping[str, Any] | None = None,
) -> list[str]:
    """Candidates of ``suite`` whose saved score is missing or stale.

    Stale: the telemetry changed since the score was taken (compared with the stamp
    ``score_run`` saved, not with file times, which another candidate's scoring moves),
    the score has errors, is incomplete or has no primary metric, ``scoring_version``
    (the suite's current rules) differs from the one the score was made with, or
    ``scoring_options`` (the suite's current options, see :func:`scoring_options`)
    differ from the ones in its stamp (a stamp without them counts as none).
    """
    entry = read_manifest(run_dir)["suites"][suite]
    scored = _read_json(run_dir / SCORES_DIR / f"{suite}.json") or {}
    return [
        c
        for c in entry["candidates"]
        if c not in scored
        or _stale(scored[c], telemetry_stamp(run_dir, suite, c), scoring_version, scoring_options)
    ]


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
    """Paired cluster bootstrap and sign-flip test of a - b on the primary metric, over the
    units both scored (clusters, groups and strata as the suite defined them)."""
    metric = a.primary
    common = sorted(set(a.units) & set(b.units))
    xs = [a.units[u].get(metric) for u in common]
    ys = [b.units[u].get(metric) for u in common]
    groups = [a.unit_groups.get(u, "") for u in common] if a.unit_groups else None
    clusters = [a.unit_clusters.get(u, u) for u in common] if a.unit_clusters else None
    strata = None
    if clusters is not None and a.cluster_strata:
        strata = [a.cluster_strata.get(c, "") for c in clusters]
    return paired_bootstrap(xs, ys, groups=groups, clusters=clusters, strata=strata, n=n, seed=seed)


# --------------------------------------------------------------------------- report


def _paired_dict(r: PairedResult) -> dict[str, Any]:
    d = {k: None if isinstance(v, float) and math.isnan(v) else v for k, v in asdict(r).items()}
    d["significant"] = r.significant if d["p_value"] is not None else None
    return d


def build_summary(run_dir: Path, *, n_boot: int = 10_000, seed: int = 0) -> dict[str, Any]:
    manifest = read_manifest(run_dir)
    suites: dict[str, Any] = {}
    for name, entry in manifest["suites"].items():
        scores = load_scores(run_dir, name)
        ranking = rank(scores)
        best = ranking[0] if ranking and _primary_mean(scores[ranking[0]]) is not None else None
        paired = {
            c: _paired_dict(compare(scores[c], scores[best], n=n_boot, seed=seed))
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
            for key in ("units", "unit_groups", "unit_clusters", "cluster_strata"):
                d.pop(key)  # per-unit data stays in scores/<suite>.json
            compact[c] = d
        suites[name] = {
            "fingerprint": entry["fingerprint"],
            "n_samples": entry["n_samples"],
            "candidates": entry["candidates"],
            "unscored": sorted(set(entry["candidates"]) - set(scores)),
            "scoring_versions": sorted({s.scoring_version or 1 for s in scores.values()}),
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


def _num_text(x: float, pct: bool, sign: str = "") -> str:
    scale, digits = (100.0, 1) if pct else (1.0, 3)
    return f"{x * scale:{sign}.{digits}f}"


def _fmt(est: Mapping[str, Any] | None, pct: bool, *, signed: bool = False) -> str:
    if not est or est.get("mean") is None:
        return "–"
    out = _num_text(est["mean"], pct, "+" if signed else "") + ("%" if pct else "")
    if est.get("low") is not None and est.get("high") is not None:
        out += f" [{_num_text(est['low'], pct)}, {_num_text(est['high'], pct)}]"
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


def _at_least(text: str | None, lower_bound: bool) -> str | None:
    return f"≥{text}" if text is not None and lower_bound else text


def _significance(p: Mapping[str, Any]) -> str | None:
    if "significant" in p:  # decided by the sign-flip test
        sig = p["significant"]
        return None if sig is None else ("yes" if sig else "no")
    if p["low"] is None or p["high"] is None:
        return None
    return "yes" if not p["low"] <= 0 <= p["high"] else "no"


def _throughput_section(tp: Mapping[str, Mapping[str, Any]]) -> list[str]:
    out = ["", "### Throughput and failure modes", ""]
    rows = []
    for c, t in tp.items():
        basis, lower = t.get("gen_tok_s_basis"), bool(t.get("lower_bound"))
        speed = None if t["gen_tok_s"] is None else str(t["gen_tok_s"])
        retried = _pct(t.get("retried_rate"))
        rows.append(
            [
                c,
                t["pages"],
                t["median_s"],
                t["p90_s"],
                _at_least(speed, basis not in (None, "decode")),
                t["peak_memory_gb"],
                _at_least(_pct(t.get("first_truncation_rate")), lower),
                _pct(t["truncation_rate"]),
                "n/a" if retried is None and t.get("ok") else _at_least(retried, lower),
                _pct(t["empty_rate"]),
                t["errors"],
            ]
        )
    header = ["candidate", "pages", "median s/page", "p90 s/page", "gen tok/s", "peak GB"]
    header += ["truncated 1st try", "truncated final", "retried", "empty", "errors"]
    out += _table(header, rows)
    notes = []
    if any(t.get("gen_tok_s_basis") in ("legacy", "end-to-end") for t in tp.values()):
        notes.append(
            "≥ tok/s: decode time was not measured, so this is generated tokens over the "
            "whole page time (prefill included; for older telemetry, only the final "
            "attempt's tokens over the time of every attempt): a lower bound."
        )
    if any(t.get("lower_bound") for t in tp.values()):
        notes.append(
            "≥ / n/a: telemetry written before the retry ladder was recorded. A page whose "
            "first attempt hit max_tokens and whose retry finished looks clean there, so "
            "first-try truncation is a lower bound and retries are unknown."
        )
    if notes:
        out += ["", *(f"_{n}_" for n in notes)]
    return out


def _cluster_notes(scores: Mapping[str, Any], ranking: Sequence[str], pct: bool) -> list[str]:
    """What the CIs resample, and the official scorer's own interval where there is one."""
    out = []
    details = [scores[c]["details"] for c in ranking]
    clustered = next((d for d in details if d.get("n_clusters")), None)
    if clustered is not None:
        what = clustered.get("clusters") or "a group of correlated units"
        out += ["", f"One cluster = {what}; {clustered['n_clusters']} clusters."]
    official = [c for c in ranking if (scores[c]["details"].get("official_ci") or [None])[0]]
    if official:
        method = scores[official[0]]["details"].get("official_ci_method") or "the scorer's"
        out += ["", f"Official scorer CI, to compare with the leaderboard's ±: {method}.", ""]
        rows = []
        for c in official:
            lo, hi = scores[c]["details"]["official_ci"]
            half = scores[c]["details"].get("half_width")
            ci = f"[{_num_text(lo, pct)}, {_num_text(hi, pct)}]"
            rows.append([c, ci, _pct(half) if pct and half is not None else half])
        out += _table(["candidate", "official CI", "official ±"], rows)
    return out


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
        "Intervals are 95% percentile bootstrap CIs that resample independent clusters, "
        "not single units: a synthetic page with all its degradation levels, an "
        "olmOCR-bench PDF with all its tests (within its category). Paired comparisons "
        "take the units both candidates scored: a cluster bootstrap CI "
        f"({n_boot} resamples, seed {summary['bootstrap']['seed']}) and a two-sided "
        "sign-flip test over clusters (every pattern up to 16 clusters, else "
        f"{n_boot} random ones); *significant* means p < 0.05. With k clusters the "
        "smallest attainable p is 2/2^k.",
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
        versions = ", ".join(map(str, s.get("scoring_versions") or [])) or "–"
        out.append(
            f"Fingerprint `{s['fingerprint'][:12]}`, {s['n_samples']} samples, "
            f"scoring version {versions}."
        )
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
                ["candidate", *metrics, "outputs"],
                (
                    [
                        c,
                        *(_fmt(scores[c]["metrics"].get(k), pct) for k in metrics),
                        f"{scores[c]['n_outputs']}/{scores[c]['n_samples']}",
                    ]
                    for c in s["ranking"]
                ),
            )
            out += _cluster_notes(scores, s["ranking"], pct)
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
                ref = "highest" if hib else "lowest"
                out += ["", f"### Paired comparison vs `{s['best']}` ({ref} mean)", ""]
                rows = []
                for c, p in s["paired_vs_best"].items():
                    ci = {"mean": p["diff"], "low": p["low"], "high": p["high"]}
                    rows.append(
                        [
                            c,
                            _fmt(ci, pct, signed=True),
                            _pvalue(p["p_value"], n_boot),
                            _significance(p),
                            p["n"],
                            p.get("n_clusters"),
                        ]
                    )
                out += _table(
                    [
                        "candidate",
                        f"Δ {primary} (candidate − `{s['best']}`) [CI]",
                        "p",
                        "significant",
                        "pairs",
                        "clusters",
                    ],
                    rows,
                )
                few = [p for p in s["paired_vs_best"].values() if p.get("n_clusters")]
                if any(p["n_clusters"] < MIN_CLUSTERS for p in few):
                    out += [
                        "",
                        f"_Fewer than {MIN_CLUSTERS} clusters: the bootstrap CI is too narrow to "
                        "rely on; significance comes from the sign-flip test, which cannot go "
                        "below p = 2/2^k with k clusters._",
                    ]
        out += _throughput_section(s["throughput"])
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
