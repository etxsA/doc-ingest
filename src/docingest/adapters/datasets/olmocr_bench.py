"""olmOCR-bench (allenai/olmOCR-bench) as a benchmark suite, scored by the official scorer.

The benchmark is unit tests, not edit distance: each single-page PDF carries pass/fail
checks (text present / absent, reading order, table-cell neighbours, equations compared
after KaTeX rendering, a baseline sanity check). We

* download the jsonl test files of a pinned dataset revision and a seeded sample of N
  PDFs per category (a seeded shuffle, so a smaller N is a subset of a larger one),
* render each PDF so its long side is ~2048 px (the OCR profile then fits it per model),
* lay candidate outputs out exactly as the scorer expects
  (``<candidate>/<pdf path without .pdf>_pg1_repeat1.md``), and
* run ``olmocr.bench.benchmark`` from its own venv (scripts/setup_bench_scorer.sh) and
  parse its stdout.

Reimplementing the tests here would silently drift from the published leaderboard.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from ...application.metrics import bootstrap_ci
from ...domain.errors import DocumentOpenError, SourceUnavailableError
from ...ports import Estimate, Sample, SuiteScore

REPO_ID = "allenai/olmOCR-bench"
# jsonl stems; the jsonl "pdf" field gives each PDF's path under bench_data/pdfs/.
CATEGORIES = (
    "arxiv_math",
    "old_scans_math",
    "table_tests",
    "old_scans",
    "headers_footers",
    "multi_column",
    "long_tiny_text",
)
SUBSET_META = "subset.json"  # written last: marks a complete subset directory
BASELINE = "baseline"  # the scorer's extra per-PDF sanity test group
METRIC = "pass_rate"

Log = Callable[[str], None]


# --------------------------------------------------------------------------- dataset subset


def subset_id(revision: str, categories: Sequence[str], per_category: int | None, seed: int) -> str:
    cats = hashlib.sha1(",".join(sorted(categories)).encode()).hexdigest()[:6]
    return f"{revision[:8]}-n{per_category or 'all'}-s{seed}-{cats}"


def _hf_file(repo_id: str, revision: str, filename: str, *, offline: bool) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(
            repo_id,
            filename,
            repo_type="dataset",
            revision=revision,
            local_files_only=offline,
        )
    )


def _fetch(repo_id: str, revision: str, filenames: list[str]) -> dict[str, Path]:
    """Offline-first: only files missing from the HF cache touch the network."""
    from huggingface_hub import snapshot_download

    found, missing = {}, []
    for name in filenames:
        try:
            found[name] = _hf_file(repo_id, revision, name, offline=True)
        except Exception:
            missing.append(name)
    if missing:
        try:
            snapshot_download(
                repo_id, repo_type="dataset", revision=revision, allow_patterns=missing
            )
            found.update({n: _hf_file(repo_id, revision, n, offline=True) for n in missing})
        except Exception as e:
            raise SourceUnavailableError(
                f"cannot download {len(missing)} files of {repo_id}@{revision[:8]}: {e}"
            ) from e
    return found


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_subset(
    root: Path,
    *,
    revision: str,
    categories: Sequence[str] = CATEGORIES,
    per_category: int | None = None,
    seed: int = 0,
    repo_id: str = REPO_ID,
    log: Log = print,
) -> Path:
    """Build ``root/<subset-id>/`` (filtered jsonl files + pdfs/ + subset.json); idempotent.

    ``per_category=None`` (or 0) keeps every PDF: the full benchmark.
    """
    if bad := [c for c in categories if c not in CATEGORIES]:
        raise ValueError(f"unknown olmOCR-bench categories {bad}; choose from {CATEGORIES}")
    out = root / subset_id(revision, categories, per_category, seed)
    if (out / SUBSET_META).exists():
        return out
    log(f"olmOCR-bench: building subset {out.name} ({repo_id}@{revision[:8]})")
    jsonls = _fetch(repo_id, revision, [f"bench_data/{c}.jsonl" for c in categories])
    picks: dict[str, list[str]] = {}
    out.mkdir(parents=True, exist_ok=True)
    for cat in categories:
        lines = jsonls[f"bench_data/{cat}.jsonl"].read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        pdfs = sorted({r["pdf"] for r in rows})
        random.Random(f"{seed}:{cat}").shuffle(pdfs)  # str seeds are stable across runs
        keep = pdfs[:per_category] if per_category else pdfs
        picks[cat] = sorted(keep)
        chosen = set(keep)
        with (out / f"{cat}.jsonl").open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows if r["pdf"] in chosen)
        log(f"  {cat}: {len(keep)}/{len(pdfs)} PDFs")
    all_pdfs = [p for cat in categories for p in picks[cat]]
    files = _fetch(repo_id, revision, [f"bench_data/pdfs/{p}" for p in all_pdfs])
    for p in all_pdfs:
        dst = out / "pdfs" / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(files[f"bench_data/pdfs/{p}"], dst)
    meta = {
        "repo_id": repo_id,
        "revision": revision,
        "categories": list(categories),
        "per_category": per_category or None,
        "seed": seed,
        "jsonl": {f"{c}.jsonl": _sha256(out / f"{c}.jsonl") for c in categories},
        "pdfs": {p: _sha256(out / "pdfs" / p) for p in all_pdfs},
    }
    (out / SUBSET_META).write_text(json.dumps(meta, indent=2))
    return out


# --------------------------------------------------------------------------- scorer stdout


@dataclass
class ScorerResult:
    """What ``olmocr.bench.benchmark`` printed for one candidate (rates are fractions)."""

    candidate: str
    overall: float | None = None  # mean of per-jsonl pass rates (the leaderboard number)
    half_width: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    n_tests: int | None = None
    by_jsonl: dict[str, tuple[int, int]] = field(default_factory=dict)  # (passed, total)
    by_type: dict[str, tuple[float, int]] = field(default_factory=dict)  # (rate, tests)
    failed: list[str] = field(default_factory=list)  # "<test id> on <pdf> page N ..." lines
    errors: list[str] = field(default_factory=list)


_CANDIDATE = re.compile(r"^Candidate: (?P<name>\S+)\s*$")
_FAIL = re.compile(r"^\s+\[FAIL\] Test (?P<rest>.+)$")
_ERROR = re.compile(r"^\s+\[ERROR\] (?P<msg>.+)$")
_AVERAGE = re.compile(
    r"^\s+Average Score: [\d.]+% \(\d+% CI: \[(?P<lo>[\d.]+)%, (?P<hi>[\d.]+)%\]\) "
    r"over (?P<n>\d+) tests\."
)
_FINAL_HEADER = "Final Summary with"
_FINAL = re.compile(
    r"^(?P<name>\S+)\s*: Average Score: (?:FAILED \(errors\)|(?P<score>[\d.]+)%)"
    r"(?:\s*± (?P<hw>[\d.]+)%)?"
)
_TYPE = re.compile(r"^ {4}(?P<type>\S+)\s*: (?P<rate>[\d.]+)% average pass rate over (?P<n>\d+)")
_JSONL = re.compile(r"^ {8}(?P<file>\S+)\s*: [\d.]+% \((?P<passed>\d+)/(?P<total>\d+) tests\)")


def _frac(percent: str) -> float:
    return round(float(percent) / 100, 4)


def parse_scorer_stdout(text: str) -> dict[str, ScorerResult]:
    """Parse the per-candidate sections and the final summary of the scorer's stdout."""
    results: dict[str, ScorerResult] = {}
    section, cur = None, None
    for line in text.splitlines():
        if line.startswith(_FINAL_HEADER):
            section, cur = "final", None
            continue
        if section != "final" and (m := _CANDIDATE.match(line)):
            section = "candidate"
            cur = results.setdefault(m["name"], ScorerResult(m["name"]))
            continue
        if section == "candidate" and cur is not None:
            if m := _FAIL.match(line):
                cur.failed.append(m["rest"])
            elif m := _ERROR.match(line):
                cur.errors.append(m["msg"])
            elif m := _AVERAGE.match(line):
                cur.ci_low, cur.ci_high = _frac(m["lo"]), _frac(m["hi"])
                cur.n_tests = int(m["n"])
        elif section == "final":
            if m := _FINAL.match(line):
                cur = results.setdefault(m["name"], ScorerResult(m["name"]))
                cur.overall = _frac(m["score"]) if m["score"] else None
                cur.half_width = _frac(m["hw"]) if m["hw"] else None
            elif cur is not None and (m := _TYPE.match(line)):
                cur.by_type[m["type"]] = (_frac(m["rate"]), int(m["n"]))
            elif cur is not None and (m := _JSONL.match(line)):
                cur.by_jsonl[m["file"].removesuffix(".jsonl")] = (int(m["passed"]), int(m["total"]))
    return results


def failed_test_ids(failed_lines: Sequence[str], known_ids: set[str]) -> set[str]:
    """Test ids of "[FAIL] Test <id> on <pdf> page N ..." lines; ids are matched against the
    known ids because nothing stops an id from containing " on "."""
    out = set()
    for rest in failed_lines:
        cuts = [m.start() for m in re.finditer(" on ", rest)]
        match = next((rest[:i] for i in cuts if rest[:i] in known_ids), None)
        if match is None and cuts:
            match = rest[: cuts[0]]
        if match is not None:
            out.add(match)
    return out


# --------------------------------------------------------------------------- suite


@dataclass(frozen=True)
class ScorerConfig:
    python: Path  # the scorer venv's interpreter (scripts/setup_bench_scorer.sh)
    playwright_browsers: Path | None = None  # PLAYWRIGHT_BROWSERS_PATH (math tests)
    home: Path | None = None  # HOME for the scorer: its equation cache lives under it
    timeout_s: float = 3600
    bootstrap_samples: int = 1000
    confidence_level: float = 0.95
    skip_baseline: bool = False


def render_first_page(path: Path, long_side: int) -> Image.Image:
    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as e:
        raise DocumentOpenError(f"cannot open {path.name}: {e}") from e
    try:
        page = pdf[0]
        scale = long_side / max(page.get_size())
        return page.render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()


@dataclass(frozen=True)
class _Test:
    id: str
    group: str  # jsonl stem, or "baseline"
    type: str
    pdf: str


class OlmOcrBenchSuite:
    name = "olmocr-bench"

    def __init__(
        self,
        subset_dir: Path,
        *,
        long_side: int = 2048,
        scorer: ScorerConfig | None = None,
        log: Log = print,
    ):
        meta_path = subset_dir / SUBSET_META
        if not meta_path.exists():
            raise DocumentOpenError(f"{subset_dir} is not a prepared olmOCR-bench subset")
        self.subset_dir = subset_dir
        self.meta = json.loads(meta_path.read_text())
        self.long_side = long_side
        self.scorer = scorer
        self.log = log
        definition = {"subset": self.meta, "long_side": long_side, "pdfium": version("pypdfium2")}
        blob = json.dumps(definition, sort_keys=True).encode()
        self.fingerprint = hashlib.sha256(blob).hexdigest()
        self._samples: list[Sample] | None = None

    def _rows(self) -> dict[str, list[dict]]:
        rows = {}
        for cat in self.meta["categories"]:
            text = (self.subset_dir / f"{cat}.jsonl").read_text(encoding="utf-8")
            rows[cat] = [json.loads(line) for line in text.splitlines() if line.strip()]
        return rows

    def samples(self) -> list[Sample]:
        if self._samples is None:
            seen: dict[str, Sample] = {}
            for cat, rows in self._rows().items():
                n_tests: dict[str, int] = defaultdict(int)
                for r in rows:
                    n_tests[r["pdf"]] += 1
                for pdf in sorted(n_tests):
                    if pdf in seen:
                        continue
                    seen[pdf] = Sample(
                        id=pdf.removesuffix(".pdf"),
                        category=cat,
                        load_image=partial(
                            render_first_page, self.subset_dir / "pdfs" / pdf, self.long_side
                        ),
                        extra={"pdf": pdf, "tests": n_tests[pdf]},
                    )
            self._samples = list(seen.values())
        return self._samples

    def output_path(self, run_dir: Path, candidate: str, sample: Sample) -> Path:
        stem = str(sample.extra["pdf"]).removesuffix(".pdf")
        return run_dir / self.name / candidate / f"{stem}_pg1_repeat1.md"

    def tests(self) -> list[_Test]:
        """Every test the scorer runs, including the baseline test it adds per PDF."""
        tests, has_baseline = [], set()
        for cat, rows in self._rows().items():
            for r in rows:
                tests.append(_Test(r["id"], cat, r["type"], r["pdf"]))
                if r["type"] == BASELINE:
                    has_baseline.add(r["pdf"])
        for s in self.samples():
            pdf = str(s.extra["pdf"])
            if pdf not in has_baseline:
                tests.append(_Test(f"{pdf}_baseline", BASELINE, BASELINE, pdf))
        skip = self.scorer is not None and self.scorer.skip_baseline
        return [t for t in tests if not (skip and t.type == BASELINE)]

    def scorer_dir(self, run_dir: Path) -> Path:
        """``run_dir/olmocr-bench``: jsonl copies + ``pdfs`` symlink + one dir per candidate,
        the layout ``olmocr.bench.benchmark --dir`` expects."""
        view = run_dir / self.name
        view.mkdir(parents=True, exist_ok=True)
        for jl in sorted(self.subset_dir.glob("*.jsonl")):
            shutil.copyfile(jl, view / jl.name)
        link, target = view / "pdfs", os.path.relpath(self.subset_dir / "pdfs", view)
        if link.is_symlink() and os.readlink(link) != target:
            link.unlink()
        if not link.is_symlink() and not link.exists():
            link.symlink_to(target, target_is_directory=True)
        return view

    def _scorer_version(self) -> str | None:
        assert self.scorer is not None
        code = "import importlib.metadata as m; print(m.version('olmocr'))"
        try:
            out = subprocess.run(
                [str(self.scorer.python), "-c", code],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() or None

    def _run_scorer(self, view: Path, candidate: str) -> subprocess.CompletedProcess[str]:
        sc = self.scorer
        assert sc is not None
        cmd = [
            str(sc.python), "-m", "olmocr.bench.benchmark",
            "--dir", str(view),
            "--candidate", candidate,
            "--bootstrap_samples", str(sc.bootstrap_samples),
            "--confidence_level", str(sc.confidence_level),
        ]  # fmt: skip
        if sc.skip_baseline:
            cmd.append("--skip_baseline")
        env = {**os.environ, "TQDM_DISABLE": "1", "PYTHONUNBUFFERED": "1"}
        if sc.playwright_browsers is not None:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(sc.playwright_browsers)
        if sc.home is not None:
            sc.home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(sc.home)
        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=sc.timeout_s, check=False
        )

    def score(self, run_dir: Path, candidates: list[str]) -> dict[str, SuiteScore]:
        samples, tests = self.samples(), self.tests()
        view = self.scorer_dir(run_dir)
        logs = run_dir / "scores" / self.name
        installed = self.scorer is not None and self.scorer.python.exists()
        olmocr = self._scorer_version() if installed else None
        out = {}
        for cand in candidates:
            done = sum(self.output_path(run_dir, cand, s).exists() for s in samples)
            base = SuiteScore(
                primary=METRIC,
                higher_is_better=True,
                metrics={METRIC: Estimate(None)},
                n_outputs=done,
                n_samples=len(samples),
                details={"olmocr": olmocr},
            )
            out[cand] = base
            if done < len(samples):
                base.errors.append(
                    f"{len(samples) - done} of {len(samples)} PDFs have no output yet; "
                    "the official scorer needs all of them (resume the run)"
                )
                continue
            if not installed:
                base.errors.append(
                    "olmOCR-bench scorer not installed: run scripts/setup_bench_scorer.sh "
                    "(or point scorer_python at an existing scorer venv)"
                )
                continue
            self.log(f"[{self.name}] scoring {cand} with olmocr {olmocr}")
            try:
                proc = self._run_scorer(view, cand)
            except subprocess.TimeoutExpired as e:
                base.errors.append(f"scorer timed out after {e.timeout:g}s")
                continue
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / f"{cand}.log"
            log_path.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
            base.details["log"] = str(log_path.relative_to(run_dir))
            parsed = parse_scorer_stdout(proc.stdout).get(cand)
            if proc.returncode != 0 or parsed is None or parsed.overall is None:
                lines = proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines()
                tail = lines[-1] if lines else "no output"
                errs = parsed.errors[:3] if parsed else []
                base.errors.append(f"scorer failed (exit {proc.returncode}): {tail}")
                base.errors.extend(errs)
                continue
            out[cand] = self._to_score(parsed, tests, base)
        return out

    def _to_score(self, r: ScorerResult, tests: list[_Test], base: SuiteScore) -> SuiteScore:
        failed = failed_test_ids(r.failed, {t.id for t in tests})
        units: dict[str, dict[str, float | None]] = {
            t.id: {METRIC: 0.0 if t.id in failed else 1.0} for t in tests
        }
        groups = {t.id: t.group for t in tests}
        by_cat, warnings = {}, []
        for group, (passed, total) in sorted(r.by_jsonl.items()):
            vals = [float(units[t][METRIC] or 0.0) for t in units if groups[t] == group]
            if len(vals) != total or sum(vals) != passed:  # parser / layout drift guard
                warnings.append(
                    f"{group}: scorer reports {passed}/{total}, "
                    f"per-test parse gives {sum(vals):g}/{len(vals)}"
                )
            lo = hi = None
            if vals:
                _, lo, hi = (round(x, 4) for x in bootstrap_ci(vals))
            by_cat[group] = {METRIC: Estimate(round(passed / total, 4), lo, hi, total)}
        base.metrics = {METRIC: Estimate(r.overall, r.ci_low, r.ci_high, r.n_tests or len(units))}
        base.by_category = by_cat
        base.units, base.unit_groups = units, groups
        base.details.update(
            half_width=r.half_width,
            by_type={t: {"rate": rate, "n": n} for t, (rate, n) in sorted(r.by_type.items())},
            failed_tests=len(failed),
            warnings=warnings,
        )
        return base
