"""Regressions of the core review: config loading, QA LLM parameters, and the metadata
refresh / degraded-result handling as seen on disk through the real FilesystemStore."""

import json
import tomllib
from pathlib import Path

import pytest
from fakes import FakeConverter, FakeDetector, FakeImages, FakeOcr, FakePdfReader

from docingest import __version__
from docingest.adapters.qa import paperqa
from docingest.adapters.store.filesystem import DEGRADED_DIR, FilesystemStore
from docingest.application.ingest import PIPELINE_VERSION, IngestService, retitle_markdown
from docingest.config import DEFAULT_LLM_BASE, AppConfig, load_config
from docingest.domain.models import (
    DocumentManifest,
    PageMethod,
    PageRecord,
    SourceKind,
    SourceMetadata,
)
from docingest.domain.routing import RoutingPolicy
from docingest.domain.text import render_markdown
from docingest.ports import Segment

# ------------------------------------------------------------------------- config


def test_an_explicit_config_path_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"remote-ocr\.tmol"):
        load_config(tmp_path / "remote-ocr.tmol")


def test_a_missing_default_config_means_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("docingest.config.DEFAULT_CONFIG", tmp_path / "pipeline.toml")
    assert load_config() == AppConfig()


def test_an_existing_explicit_config_is_read(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[adapters]\nocr = "openai-compatible"\n')
    assert load_config(path).adapters.ocr == "openai-compatible"


def test_pipeline_version_is_the_package_and_lockfile_version():
    # Three literals kept equal by hand: the cache key, the wheel and the lock that CI
    # installs with --frozen (which does not check the lock against pyproject.toml).
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    locked = tomllib.loads((root / "uv.lock").read_text())["package"]
    (lock_version,) = [p["version"] for p in locked if p["name"] == "docingest"]
    assert project["version"] == lock_version == PIPELINE_VERSION == __version__


# ------------------------------------------------------------------- QA LLM params


@pytest.fixture
def qa_env(monkeypatch):
    for var in ("DOCINGEST_LLM", "DOCINGEST_LLM_SERVE_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paperqa, "llm_path", lambda cfg: "/hf/snapshots/qwen")
    return monkeypatch


def _cfg(**qa) -> AppConfig:
    return AppConfig.model_validate({"qa": qa})


def test_default_local_model_gets_the_local_server_and_a_placeholder_key(qa_env):
    params = paperqa.llm_params(_cfg())
    assert params["model"] == "openai//hf/snapshots/qwen"
    assert params["api_base"] == DEFAULT_LLM_BASE and params["api_key"] == "sk-local"
    qa_env.setenv("OPENAI_API_KEY", "sk-REAL-KEY")  # never sent to the local server
    assert paperqa.llm_params(_cfg())["api_key"] == "sk-local"


def test_the_served_model_path_is_not_the_litellm_model(qa_env):
    # Old: serve_llm.sh and the adapter shared DOCINGEST_LLM, so a snapshot path set for
    # the server became the litellm model string of `ask`.
    qa_env.setenv("DOCINGEST_LLM_SERVE_MODEL", "/models/other")
    assert paperqa.llm_params(_cfg())["model"] == "openai//hf/snapshots/qwen"
    qa_env.setenv("DOCINGEST_LLM", "ollama/llama3.1")
    assert paperqa.llm_params(_cfg())["model"] == "ollama/llama3.1"


def test_a_real_openai_key_is_not_overridden(qa_env):
    qa_env.setenv("OPENAI_API_KEY", "sk-REAL-KEY")
    params = paperqa.llm_params(_cfg(llm="openai/gpt-4o-mini"))
    assert "api_key" not in params  # litellm reads OPENAI_API_KEY itself
    assert "api_base" not in params  # the provider's own endpoint


def test_a_hosted_model_is_not_sent_to_the_local_server(qa_env):
    params = paperqa.llm_params(_cfg(llm="anthropic/claude-sonnet-4-5"))
    assert params == {"model": "anthropic/claude-sonnet-4-5", "max_tokens": 1024}


def test_a_keyless_local_openai_compatible_server_still_works(qa_env):
    params = paperqa.llm_params(_cfg(llm="openai/local-model", llm_base="http://127.0.0.1:1234/v1"))
    assert params["api_base"] == "http://127.0.0.1:1234/v1" and params["api_key"] == "sk-local"
    ollama = paperqa.llm_params(_cfg(llm="ollama/llama3.1", llm_base="http://localhost:11434"))
    assert ollama["api_base"] == "http://localhost:11434" and "api_key" not in ollama


# ---------------------------------------------------------------- retitle_markdown


def _manifest(title: str | None, n: int) -> DocumentManifest:
    return DocumentManifest(
        doc_id="d" * 64,
        source_path="/x/p.tex",
        source_name="p.tex",
        source_kind=SourceKind.LATEX,
        mime="application/x-tex",
        size_bytes=1,
        n_pages=n,
        source_pages=n,
        pages=[
            PageRecord(index=i, method=PageMethod.LATEX, n_chars=1, seconds=0) for i in range(n)
        ],
        pipeline_version="t",
        config_hash="h",
        title=title,
    )


@pytest.mark.parametrize("old", ["p", None, "Title: with # and\nnewline"])
@pytest.mark.parametrize("new", ["Attention Is All You Need", None])
@pytest.mark.parametrize("texts", [[], ["  intro\n\n", "# not a title\n\nbody"]])
def test_retitle_equals_rendering_again_under_the_new_title(old, new, texts):
    before = render_markdown(_manifest(old, len(texts)), texts)
    after = render_markdown(_manifest(new, len(texts)), texts)
    assert retitle_markdown(before, old, new) == after


def test_retitle_refuses_markdown_it_did_not_write():
    assert retitle_markdown("# Edited by hand\n\nbody\n", "p", "New") is None


# ----------------------------------------------------- through the FilesystemStore


def _service(root: Path, conv: FakeConverter) -> IngestService:
    return IngestService(
        detector=FakeDetector(),
        pdf=FakePdfReader(),
        ocr=FakeOcr(),
        images=FakeImages(),
        converters={SourceKind.LATEX: conv},
        store=FilesystemStore(root),
        policy=RoutingPolicy(),
        log=lambda _: None,
    )


def test_a_later_sidecar_updates_manifest_markdown_and_index_on_disk(tmp_path):
    conv = FakeConverter([Segment("Some body text.", "Intro")])
    svc = _service(tmp_path / "out", conv)
    src = tmp_path / "paper.tex"
    src.write_text("\\documentclass{article}")
    first = svc.ingest(src)
    meta = SourceMetadata(
        title="Attention Is All You Need", authors=["A. Vaswani", "N. Shazeer"], year=2017
    )
    (tmp_path / "paper.tex.meta.json").write_text(meta.model_dump_json())
    second = svc.ingest(src)
    assert conv.calls == 1 and second.location == first.location
    out = Path(second.location)
    on_disk = DocumentManifest.model_validate_json((out / "manifest.json").read_text())
    assert on_disk.metadata == meta and on_disk.title == meta.title
    assert on_disk.created_at == first.manifest.created_at  # still the original run
    assert (out / "document.md").read_text() == (
        "# Attention Is All You Need\n\n<!-- page 1 | method=latex -->\nSome body text.\n"
    )
    index = json.loads((tmp_path / "out" / "index.json").read_text())
    assert index[first.manifest.doc_id]["citation"] == (
        "Vaswani et al. (2017). Attention Is All You Need"
    )


def test_a_degraded_conversion_stays_out_of_the_cache_and_the_index(tmp_path):
    conv = FakeConverter([Segment("plain")], method=PageMethod.LATEX_PLAINTEXT, degraded=True)
    root = tmp_path / "out"
    svc = _service(root, conv)
    src = tmp_path / "paper.tex"
    src.write_text("\\documentclass{article}")
    doc = svc.ingest(src)
    assert Path(doc.location).parent == root / DEGRADED_DIR
    assert not (root / "index.json").exists()
    assert not (root / doc.manifest.doc_id[:16]).exists()
    docs, warnings = svc.store.corpus()
    assert [d.location for d in docs] == [doc.location] and "degraded" in warnings[0]
    svc.ingest(src)
    assert conv.calls == 2  # retried
