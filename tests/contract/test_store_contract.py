"""DocumentStore contract: every store implementation must pass the same tests."""

import pytest
from fakes import InMemoryStore

from docingest.adapters.store.filesystem import FilesystemStore
from docingest.domain.models import (
    DocumentManifest,
    PageMethod,
    PageRecord,
    SourceKind,
    SourceMetadata,
)


@pytest.fixture(params=["memory", "filesystem"])
def store(request, tmp_path):
    return InMemoryStore() if request.param == "memory" else FilesystemStore(tmp_path / "out")


def manifest(n_pages=2, source_pages=2, max_pages=None, ocr_all=False, config_hash="h1"):
    return DocumentManifest(
        doc_id="d" * 64,
        source_path="/x/a.pdf",
        source_name="a.pdf",
        source_kind=SourceKind.PDF,
        mime="application/pdf",
        size_bytes=10,
        n_pages=n_pages,
        source_pages=source_pages,
        max_pages=max_pages,
        ocr_all=ocr_all,
        pages=[
            PageRecord(index=i, method=PageMethod.TEXT_LAYER, n_chars=1, seconds=0)
            for i in range(n_pages)
        ],
        pipeline_version="t",
        config_hash=config_hash,
    )


def test_save_then_lookup_roundtrip(store):
    saved = store.save(manifest(), "# a\n")
    assert saved.canonical
    hit = store.lookup("d" * 64, "h1", max_pages=None, ocr_all=False)
    assert hit and hit.manifest == saved.manifest
    assert store.markdown(hit) == "# a\n"


def test_config_hash_mismatch_is_a_miss(store):
    store.save(manifest(), "# a\n")
    assert store.lookup("d" * 64, "other", max_pages=None, ocr_all=False) is None


def test_partial_run_never_replaces_complete(store):
    store.save(manifest(), "# full\n")
    part = store.save(manifest(n_pages=1, max_pages=1), "# part\n")
    assert not part.canonical
    full = store.lookup("d" * 64, "h1", max_pages=None, ocr_all=False)
    assert full and full.manifest.n_pages == 2
    assert store.lookup("d" * 64, "h1", max_pages=1, ocr_all=False).canonical  # served by full


def test_variant_lookup_requires_same_options(store):
    store.save(manifest(n_pages=1, max_pages=1), "# part\n")
    assert store.lookup("d" * 64, "h1", max_pages=1, ocr_all=False)
    assert store.lookup("d" * 64, "h1", max_pages=None, ocr_all=False) is None
    assert store.lookup("d" * 64, "h1", max_pages=2, ocr_all=False) is None


def test_corpus_prefers_canonical_and_warns_on_partial_only(store):
    store.save(manifest(n_pages=1, max_pages=1), "# part\n")
    docs, warnings = store.corpus()
    assert len(docs) == 1 and not docs[0].canonical and warnings
    store.save(manifest(), "# full\n")
    docs, warnings = store.corpus()
    assert len(docs) == 1 and docs[0].canonical and not warnings


def test_saving_the_same_run_again_replaces_it_in_place(store):
    """IngestService refreshes metadata on a cache hit by saving the result again."""
    first = store.save(manifest(), "# a\n")
    updated = manifest().model_copy(
        update={"title": "New", "metadata": SourceMetadata(title="New", year=2017)}
    )
    second = store.save(updated, "# New\n")
    assert second.location == first.location and second.canonical
    hit = store.lookup("d" * 64, "h1", max_pages=None, ocr_all=False)
    assert hit and hit.manifest.metadata == updated.metadata and store.markdown(hit) == "# New\n"
    docs, _ = store.corpus()
    assert [d.manifest.title for d in docs] == ["New"]


def test_degraded_result_is_kept_but_never_canonical_nor_served(store):
    saved = store.save(manifest(), "# fallback\n", degraded=True)
    assert not saved.canonical and store.markdown(saved) == "# fallback\n"
    assert store.lookup("d" * 64, "h1", max_pages=None, ocr_all=False) is None
    assert store.lookup("d" * 64, "h1", max_pages=2, ocr_all=False) is None
    docs, warnings = store.corpus()  # the corpus still has the document, with a warning
    assert len(docs) == 1 and not docs[0].canonical
    assert len(warnings) == 1 and "degraded" in warnings[0]


def test_degraded_result_never_replaces_a_stored_one(store):
    store.save(manifest(n_pages=1, max_pages=1), "# part\n")
    store.save(manifest(n_pages=1, max_pages=1), "# fallback part\n", degraded=True)
    part = store.lookup("d" * 64, "h1", max_pages=1, ocr_all=False)
    assert part and store.markdown(part) == "# part\n"
    store.save(manifest(), "# good\n")
    store.save(manifest(), "# fallback\n", degraded=True)
    full = store.lookup("d" * 64, "h1", max_pages=None, ocr_all=False)
    assert full and store.markdown(full) == "# good\n"
    docs, warnings = store.corpus()
    assert len(docs) == 1 and docs[0].canonical and not warnings
