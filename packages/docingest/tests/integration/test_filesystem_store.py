import os
import stat

from builders import LONG, text_pdf
from fakes import FakeOcr

from docingest.bootstrap import Container


def test_outputs_have_normal_permissions_and_no_temp_files(cfg, tmp_path):
    svc = Container(cfg, log=lambda _: None, overrides={"ocr": FakeOcr()}).ingest
    doc = svc.ingest(text_pdf(tmp_path / "d.pdf", LONG))
    out = tmp_path / "out" / doc.manifest.doc_id[:16]
    umask = os.umask(0)
    os.umask(umask)
    for name in ("document.md", "manifest.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o666 & ~umask
    assert not list(out.glob(".*.tmp"))
    assert (tmp_path / "out" / "index.json").exists()


def test_corrupt_cached_manifest_is_a_cache_miss(cfg, tmp_path):
    svc = Container(cfg, log=lambda _: None, overrides={"ocr": FakeOcr()}).ingest
    src = text_pdf(tmp_path / "d.pdf", LONG)
    doc = svc.ingest(src)
    (tmp_path / "out" / doc.manifest.doc_id[:16] / "manifest.json").write_text('{"doc_id": "tr')
    assert svc.ingest(src).manifest.n_pages == 1  # recomputed instead of raising
