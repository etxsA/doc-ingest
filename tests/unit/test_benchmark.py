"""BenchmarkRunner, scoring persistence and report aggregation, on an in-memory suite."""

import json
import os
from pathlib import Path

import pytest
from fakes import FakeOcr
from PIL import Image

from docingest.application.benchmark import (
    BenchmarkRunner,
    RunMismatchError,
    build_summary,
    load_scores,
    needs_scoring,
    rank,
    read_manifest,
    read_telemetry,
    render_report,
    score_run,
    telemetry_path,
    throughput,
    write_report,
)
from docingest.ports import (
    BenchmarkSuite,
    CandidateSpec,
    Estimate,
    OcrResult,
    Sample,
    SuiteScore,
)

MACHINE = {"platform": "test", "machine": "arm64", "cpu": "Test CPU", "ram_gb": 8.0}


def blank() -> Image.Image:
    return Image.new("RGB", (8, 8), "white")


class MemSuite:
    """Samples whose reference is their id; score = exact-match rate."""

    name = "mem"

    def __init__(self, ids=("a", "b", "c"), broken=(), fingerprint="fp-1"):
        self.ids, self.broken, self.fingerprint = list(ids), set(broken), fingerprint

    def _load(self, sid: str) -> Image.Image:
        if sid in self.broken:
            raise OSError(f"cannot render {sid}")
        return blank()

    def samples(self) -> list[Sample]:
        return [
            Sample(i, "even" if k % 2 == 0 else "odd", lambda i=i: self._load(i), reference=i)
            for k, i in enumerate(self.ids)
        ]

    def output_path(self, run_dir: Path, candidate: str, sample: Sample) -> Path:
        return run_dir / self.name / candidate / f"{sample.id}.txt"

    def score(self, run_dir: Path, candidates: list[str]) -> dict[str, SuiteScore]:
        out = {}
        for c in candidates:
            units = {}
            for s in self.samples():
                p = self.output_path(run_dir, c, s)
                if p.exists():
                    units[s.id] = {"acc": float(p.read_text() == s.reference)}
            vals = [u["acc"] for u in units.values()]
            mean = sum(vals) / len(vals) if vals else None
            out[c] = SuiteScore(
                primary="acc",
                higher_is_better=True,
                metrics={"acc": Estimate(mean, mean, mean, len(vals))},
                by_category={"even": {"acc": Estimate(mean)}},
                units=units,
                n_outputs=len(units),
                n_samples=len(self.ids),
            )
        return out


class EchoOcr(FakeOcr):
    """Transcribes sample images tagged with their id (see TaggedSuite); can unload."""

    def __init__(self, fail_every: int = 0, finish: str = "stop"):
        super().__init__()
        self.fail_every, self.finish, self.unloaded = fail_every, finish, 0

    def transcribe(self, image):
        self.calls += 1
        if self.fail_every and self.calls % self.fail_every == 0:
            raise RuntimeError("metal out of memory")
        return OcrResult(image.info.get("id", ""), 2.0, 40, self.finish, 1.5)

    def unload(self):
        self.unloaded += 1


class TaggedSuite(MemSuite):
    def _load(self, sid):
        img = super()._load(sid)
        img.info["id"] = sid
        return img


def spec(name: str, **kw) -> CandidateSpec:
    return CandidateSpec(name, f"org/{name}", "0" * 40, **kw)


def runner(engines: dict, **kw) -> BenchmarkRunner:
    def factory(s: CandidateSpec):
        factory.built.append(s.name)
        return engines[s.name]

    factory.built = []
    r = BenchmarkRunner(factory, log=lambda _: None, environment=lambda: MACHINE, **kw)
    r.built = factory.built
    return r


def test_suite_and_spec_contracts():
    assert isinstance(MemSuite(), BenchmarkSuite)
    with pytest.raises(ValueError, match="candidate name"):
        spec("bad name")
    with pytest.raises(ValueError, match="candidate name"):
        spec("pdfs")  # would collide with olmOCR-bench's pdfs/ folder
    s = SuiteScore("acc", True, {"acc": Estimate(0.5, 0.4, 0.6, 3)}, {"x": {"acc": Estimate(1.0)}})
    assert SuiteScore.from_dict(json.loads(json.dumps(s.to_dict()))) == s


def test_run_writes_outputs_telemetry_manifest_and_unloads(tmp_path):
    engines = {"good": EchoOcr(), "other": EchoOcr()}
    r = runner(engines)
    res = r.run(TaggedSuite(), [spec("good"), spec("other")], tmp_path, settings={"k": 1})
    assert [(x.candidate, x.done, x.errors, x.remaining) for x in res] == [
        ("good", 3, 0, 0),
        ("other", 3, 0, 0),
    ]
    assert (tmp_path / "mem" / "good" / "b.txt").read_text() == "b"
    assert engines["good"].unloaded == engines["other"].unloaded == 1
    recs = read_telemetry(telemetry_path(tmp_path, "mem", "good"))
    assert [x["sample_id"] for x in recs] == ["a", "b", "c"]
    rec = recs[0]
    assert rec["category"] == "even" and rec["seconds"] == 2.0 and rec["gen_tokens"] == 40
    assert rec["finish_reason"] == "stop" and rec["peak_memory_gb"] == 1.5
    assert rec["chars"] == 1 and rec["error"] is None and rec["empty"] is False
    m = read_manifest(tmp_path)
    assert m["suites"]["mem"] == {
        "fingerprint": "fp-1",
        "settings": {"k": 1},
        "n_samples": 3,
        "candidates": ["good", "other"],
    }
    assert m["candidates"]["good"]["repo_id"] == "org/good" and m["machine"] == MACHINE
    assert m["versions"]["pipeline"] and "mlx-vlm" in m["versions"]


def test_resume_skips_existing_outputs_without_loading_the_model(tmp_path):
    suite = TaggedSuite()
    runner({"good": EchoOcr()}).run(suite, [spec("good")], tmp_path)
    (tmp_path / "mem" / "good" / "c.txt").unlink()  # interrupted before the last page
    again = runner({"good": EchoOcr()})
    [res] = again.run(suite, [spec("good")], tmp_path)
    assert (res.skipped, res.done) == (2, 1) and again.built == ["good"]
    done = runner({"good": EchoOcr()})
    [res] = done.run(suite, [spec("good")], tmp_path)
    assert res.skipped == 3 and done.built == []  # nothing to do: no engine built
    assert not list(tmp_path.rglob("*.part"))


def test_per_sample_errors_are_recorded_written_empty_and_retried(tmp_path):
    engine = EchoOcr()
    [res] = runner({"good": engine}).run(TaggedSuite(broken={"b"}), [spec("good")], tmp_path)
    assert (res.done, res.errors) == (2, 1) and engine.calls == 2
    assert (tmp_path / "mem" / "good" / "b.txt").read_text() == ""  # scored as a miss
    rec = read_telemetry(telemetry_path(tmp_path, "mem", "good"))[1]
    assert rec["error"] == "OSError: cannot render b" and rec["gen_tokens"] is None
    # A plain resume keeps the recorded failure; --retry-errors re-runs it.
    [res] = runner({"good": EchoOcr()}).run(TaggedSuite(), [spec("good")], tmp_path)
    assert res.skipped == 3
    [res] = runner({"good": EchoOcr()}).run(
        TaggedSuite(), [spec("good")], tmp_path, retry_errors=True
    )
    assert (res.skipped, res.done) == (2, 1)
    assert (tmp_path / "mem" / "good" / "b.txt").read_text() == "b"
    tp = throughput(read_telemetry(telemetry_path(tmp_path, "mem", "good")))
    assert tp["errors"] == 0 and tp["pages"] == 3  # latest attempt per sample wins


def test_consecutive_errors_stop_the_candidate_but_still_unload(tmp_path):
    engine = EchoOcr(fail_every=1)
    suite = TaggedSuite(ids=[str(i) for i in range(10)])
    [res] = runner({"bad": engine}, max_consecutive_errors=3).run(suite, [spec("bad")], tmp_path)
    assert res.errors == 3 and res.remaining == 7 and "consecutive" in (res.stopped or "")
    assert engine.unloaded == 1
    # The streak's outputs are removed: resuming (model fixed) retries those pages too.
    assert not list((tmp_path / "mem").rglob("*.txt"))
    [res] = runner({"bad": EchoOcr()}).run(suite, [spec("bad")], tmp_path)
    assert (res.skipped, res.done) == (0, 10)


def test_time_budget_caps_each_candidate(tmp_path):
    t = [0.0]

    def clock():
        t[0] += 5.0  # every clock read advances 5 s
        return t[0]

    suite = TaggedSuite(ids=[str(i) for i in range(10)])
    engines = {"x": EchoOcr(), "y": EchoOcr()}
    res = runner(engines, clock=clock).run(
        suite, [spec("x"), spec("y")], tmp_path, time_budget_s=20
    )
    for r in res:  # the budget is per candidate: both get some samples done
        assert 0 < r.done < 10 and r.remaining == 10 - r.done and "budget" in (r.stopped or "")


def test_manifest_refuses_to_mix_definitions(tmp_path):
    runner({"good": EchoOcr()}).run(TaggedSuite(), [spec("good")], tmp_path)
    with pytest.raises(RunMismatchError, match="fingerprint"):
        runner({"good": EchoOcr()}).run(TaggedSuite(fingerprint="fp-2"), [spec("good")], tmp_path)
    with pytest.raises(RunMismatchError, match="different spec"):
        runner({"good": EchoOcr()}).run(TaggedSuite(), [spec("good", max_side=900)], tmp_path)


def test_throughput_statistics():
    recs = [
        {"sample_id": "a", "seconds": 1.0, "gen_tokens": 100, "finish_reason": "stop",
         "peak_memory_gb": 2.0, "empty": False, "error": None},
        {"sample_id": "b", "seconds": 3.0, "gen_tokens": 300, "finish_reason": "length",
         "peak_memory_gb": 3.5, "empty": False, "error": None},
        {"sample_id": "c", "seconds": 2.0, "gen_tokens": 0, "finish_reason": "stop",
         "peak_memory_gb": None, "empty": True, "error": None},
        {"sample_id": "d", "seconds": 0.1, "gen_tokens": None, "finish_reason": None,
         "peak_memory_gb": None, "empty": True, "error": "RuntimeError: boom"},
    ]  # fmt: skip
    tp = throughput(recs)
    assert tp["pages"] == 4 and tp["ok"] == 3 and tp["errors"] == 1
    assert tp["median_s"] == 2.0 and tp["p90_s"] == pytest.approx(2.8)
    assert tp["gen_tok_s"] == pytest.approx(400 / 6, abs=0.1)
    assert tp["peak_memory_gb"] == 3.5
    assert tp["truncation_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert tp["empty_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert throughput([])["median_s"] is None


def test_score_report_ranks_and_compares_against_the_best(tmp_path):
    class Wrong(EchoOcr):
        def transcribe(self, image):
            res = super().transcribe(image)
            return OcrResult("nope" if res.text != "a" else "a", 1.0, 10, "length", None)

    suite = TaggedSuite(ids=[f"s{i}" for i in range(12)] + ["a"])
    engines = {"good": EchoOcr(), "bad": Wrong()}
    runner(engines).run(suite, [spec("bad"), spec("good")], tmp_path)
    scores = score_run(suite, tmp_path)
    assert rank(scores) == ["good", "bad"]
    assert load_scores(tmp_path, "mem")["good"].metrics["acc"].mean == 1.0
    # Scoring one candidate again merges into the stored scores.
    score_run(suite, tmp_path, ["bad"])
    assert set(load_scores(tmp_path, "mem")) == {"good", "bad"}

    summary = build_summary(tmp_path, n_boot=500)
    s = summary["suites"]["mem"]
    assert s["best"] == "good" and s["ranking"] == ["good", "bad"]
    p = s["paired_vs_best"]["bad"]
    assert p["diff"] == pytest.approx(-12 / 13) and p["high"] < 0 and p["n"] == 13
    assert "units" not in s["scores"]["good"]  # per-unit scores stay in scores/mem.json
    assert s["throughput"]["bad"]["truncation_rate"] == 1.0

    json_path, md_path = write_report(tmp_path, n_boot=500)
    assert json.loads(json_path.read_text())["suites"]["mem"]["best"] == "good"
    md = md_path.read_text()
    assert "## Suite `mem`" in md and "Paired comparison vs best (`good`)" in md
    assert "| 1 | good | 1.000 [1.000, 1.000] |" in md and "| bad |" in md


def test_rank_puts_incomplete_and_unscored_candidates_last():
    def sc(mean, errors=()):
        return SuiteScore("cer", False, {"cer": Estimate(mean)}, errors=list(errors))

    scores = {"x": sc(0.2), "y": sc(0.1, ["3 samples missing"]), "z": sc(None), "w": sc(0.3)}
    assert rank(scores) == ["x", "w", "y", "z"]


def test_needs_scoring_tracks_new_and_rerun_candidates(tmp_path):
    suite = TaggedSuite()
    runner({"a": EchoOcr()}).run(suite, [spec("a")], tmp_path)
    assert needs_scoring(tmp_path, "mem") == ["a"]
    score_run(suite, tmp_path)
    assert needs_scoring(tmp_path, "mem") == []
    runner({"b": EchoOcr()}).run(suite, [spec("b")], tmp_path)
    assert needs_scoring(tmp_path, "mem") == ["b"]
    score_run(suite, tmp_path, ["b"])
    tele = telemetry_path(tmp_path, "mem", "a")
    stamp = (tmp_path / "scores" / "mem.json").stat().st_mtime
    os.utime(tele, (stamp + 10, stamp + 10))  # "a" transcribed more pages after scoring
    assert needs_scoring(tmp_path, "mem") == ["a"]


def test_paired_comparison_without_common_units_is_reported_as_missing(tmp_path):
    runner({"a": EchoOcr(), "b": EchoOcr()}).run(TaggedSuite(), [spec("a"), spec("b")], tmp_path)
    (tmp_path / "scores").mkdir()
    scores = {
        "a": SuiteScore("acc", True, {"acc": Estimate(0.9)}, units={"x": {"acc": 1.0}}),
        "b": SuiteScore("acc", True, {"acc": Estimate(0.5)}, units={"y": {"acc": 0.0}}),
    }
    (tmp_path / "scores" / "mem.json").write_text(
        json.dumps({k: v.to_dict() for k, v in scores.items()})
    )
    json_path, md_path = write_report(tmp_path, n_boot=100)
    p = json.loads(json_path.read_text())["suites"]["mem"]["paired_vs_best"]["b"]
    assert p["n"] == 0 and p["diff"] is None and p["p_value"] is None
    assert "| b | – | – | – | 0 |" in md_path.read_text()


def test_report_renders_unscored_suites(tmp_path):
    runner({"good": EchoOcr()}).run(TaggedSuite(), [spec("good")], tmp_path)
    summary = build_summary(tmp_path, n_boot=100)
    assert summary["suites"]["mem"]["unscored"] == ["good"]
    md = render_report(summary)
    assert "Not scored yet" in md and "`good`: not scored" in md
