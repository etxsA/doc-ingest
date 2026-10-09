"""IndexService with fakes: incremental build, pruning, status, removal."""

import pytest
from fakes import FakeEmbedder, FakeIndex, InMemoryStore, add_doc

from docingest.application.index import IndexService, content_key
from docingest.domain.chunking import chunk_pages
from docingest.domain.errors import IndexMismatchError
from docingest.domain.models import DocumentManifest, PageMethod, PageRecord, SourceKind
from docingest.domain.text import render_markdown, split_pages

CHARS, OVERLAP = 60, 10


def remove_doc(store, doc_id):
    del store.canonical[doc_id]


PAPER_A, PAPER_B, PAPER_C = "a" * 64, "b" * 64, "c" * 64
TEXT_A = [("Attention weights use a softmax over scaled dot products. " * 4, "Method")]
TEXT_B = [
    ("Qubits decohere when they couple to the environment. " * 3, "Intro"),
    ("Smith et al. 2020. A study of coherence. [1] Jones. Noise in qubits.", "References"),
]


@pytest.fixture
def store():
    s = InMemoryStore()
    add_doc(s, PAPER_A, TEXT_A)
    add_doc(s, PAPER_B, TEXT_B)
    return s


def make(store, *, index=None, embedder=None, **kw):
    embedder = embedder or FakeEmbedder()
    index = index or FakeIndex(embedder_fingerprint=embedder.fingerprint)
    service = IndexService(
        store=store,
        embedder=embedder,
        index=index,
        chunk_chars=CHARS,
        overlap=OVERLAP,
        log=lambda _: None,
        **kw,
    )
    return service, index, embedder


def test_build_embeds_every_chunk_once_and_makes_them_searchable(store):
    service, index, embedder = make(store)
    report = service.build()
    pages = {
        d: split_pages(store.markdown(s)) for s in store.corpus()[0] for d in [s.manifest.doc_id]
    }
    expected = sum(
        len(chunk_pages(d, p, chunk_chars=CHARS, overlap=OVERLAP)) for d, p in pages.items()
    )
    assert (report.added, report.updated, report.unchanged) == (2, 0, 0)
    assert report.chunks_embedded == expected and report.committed
    assert index.keys() == {d: content_key(p) for d, p in pages.items()}
    hits = index.search("softmax", embedder.embed_query("softmax attention"), 3)
    assert hits and hits[0].chunk.doc_id == PAPER_A
    assert index.stats().searchable_documents == 2


def test_chunks_are_the_ones_ask_would_use_with_references_tagged(store):
    service, index, embedder = make(store)
    service.build()
    chunks = {h.chunk for h in index.search("x", embedder.embed_query("x"), 1000)}
    for doc in store.corpus()[0]:
        doc_id = doc.manifest.doc_id
        pages = split_pages(store.markdown(doc))
        plain = chunk_pages(doc_id, pages, chunk_chars=CHARS, overlap=OVERLAP)
        assert {(c.name, c.text, c.start) for c in chunks if c.doc_id == doc_id} == {
            (c.name, c.text, c.start) for c in plain
        }
    refs = {c for c in chunks if c.is_reference}
    assert refs and all(c.doc_id == PAPER_B and c.last_page == 2 for c in refs)
    assert any(c.doc_id == PAPER_B and not c.is_reference for c in chunks)
    assert not any(c.is_reference for c in chunks if c.doc_id == PAPER_A)


def test_a_second_build_makes_no_embedding_request(store):
    service, _, embedder = make(store)
    service.build()
    calls = embedder.calls
    report = service.build()
    assert (report.added, report.updated, report.unchanged) == (0, 0, 2)
    assert report.chunks_embedded == 0 and not report.committed
    assert embedder.calls == calls


def test_only_a_changed_document_is_embedded_again(store):
    service, index, embedder = make(store)
    service.build()
    add_doc(store, PAPER_A, [("A different paper about graphs and spectra of graphs.", "Intro")])
    report = service.build()
    assert (report.added, report.updated, report.unchanged) == (0, 1, 1)
    hits = index.search("graphs", embedder.embed_query("graphs"), 10)
    assert hits[0].chunk.doc_id == PAPER_A and "graphs" in hits[0].chunk.text
    assert not any("softmax" in h.chunk.text for h in hits)


def test_a_new_document_is_added_and_a_vanished_one_is_pruned(store):
    service, index, _ = make(store)
    service.build()
    add_doc(store, PAPER_C, [("Graph spectra and the Laplacian of a graph.", "Intro")])
    remove_doc(store, PAPER_A)
    report = service.build()
    assert (report.added, report.pruned) == (1, [PAPER_A])
    assert set(index.keys()) == {PAPER_B, PAPER_C}
    assert index.stats().searchable_documents == 2


def test_pruning_can_be_switched_off(store):
    service, index, _ = make(store)
    service.build()
    remove_doc(store, PAPER_A)
    report = service.build(prune=False)
    assert report.pruned == [] and set(index.keys()) == {PAPER_A, PAPER_B}


def test_chunks_are_embedded_in_batches_across_documents(store):
    service, index, embedder = make(store, batch_chunks=3)
    service.build()
    total = index.stats().chunks
    assert 1 < embedder.calls < total  # several calls, each holding more than one chunk
    assert set(index.keys()) == {PAPER_A, PAPER_B}


def test_an_index_of_another_embedder_is_refused_before_anything_is_embedded(store):
    embedder = FakeEmbedder(fingerprint="model-b 1")
    index = FakeIndex(embedder_fingerprint="model-a 1")
    service, _, _ = make(store, index=index, embedder=embedder)
    with pytest.raises(IndexMismatchError, match=r"model-a 1.*model-b 1"):
        service.build()
    assert embedder.calls == 0 and index.keys() == {}


def test_documents_stored_but_never_committed_become_searchable_without_re_embedding(store):
    service, index, embedder = make(store)
    for doc in store.corpus()[0]:  # an earlier, interrupted run: upserted, never committed
        pages = split_pages(store.markdown(doc))
        chunks = chunk_pages(doc.manifest.doc_id, pages, chunk_chars=CHARS, overlap=OVERLAP)
        index.upsert(
            doc.manifest.doc_id,
            content_key(pages),
            chunks,
            embedder.embed_documents([c.text for c in chunks]),
        )
    calls = embedder.calls
    report = service.build()
    assert report.unchanged == 2 and report.committed and embedder.calls == calls
    assert index.stats().searchable_documents == 2


def test_an_update_stored_but_not_committed_is_committed_by_the_next_build(store):
    service, index, embedder = make(store)
    service.build()
    add_doc(store, PAPER_A, [("A different paper about graphs and spectra of graphs.", "Intro")])
    [doc] = [d for d in store.corpus()[0] if d.manifest.doc_id == PAPER_A]
    pages = split_pages(store.markdown(doc))
    chunks = chunk_pages(PAPER_A, pages, chunk_chars=CHARS, overlap=OVERLAP)
    # the interrupted run: the new version stored under its new key, then the process died
    index.upsert(
        PAPER_A, content_key(pages), chunks, embedder.embed_documents([c.text for c in chunks])
    )
    assert index.stats().documents == index.stats().searchable_documents  # counts cannot tell
    assert not any(
        "graphs" in h.chunk.text for h in index.search("graphs", embedder.embed_query("graphs"), 10)
    )
    calls = embedder.calls
    report = service.build()
    assert (report.updated, report.unchanged) == (0, 2)  # keys() already holds the new version
    assert report.committed and embedder.calls == calls
    hits = index.search("graphs", embedder.embed_query("graphs"), 10)
    assert hits[0].chunk.doc_id == PAPER_A and "graphs" in hits[0].chunk.text
    assert index.stats().pending is False


def test_build_needs_a_corpus_and_passes_the_store_warnings_on(store):
    with pytest.raises(RuntimeError, match="no ingested documents"):
        make(InMemoryStore())[0].build()
    partial = DocumentManifest(
        doc_id=PAPER_C,
        source_path="c.txt",
        source_name="c.txt",
        source_kind=SourceKind.TEXT,
        mime="text/plain",
        size_bytes=1,
        n_pages=1,
        source_pages=2,
        max_pages=1,
        pages=[PageRecord(index=0, method=PageMethod.PASSTHROUGH, n_chars=3, seconds=0.0)],
        pipeline_version="t",
        config_hash="h",
    )
    store.save(partial, render_markdown(partial, ["abc"]))
    report = make(store)[0].build()
    assert any("partial" in w for w in report.warnings)


def test_a_document_without_text_is_recorded_so_it_is_not_retried(store):
    add_doc(store, PAPER_C, [("", "Empty")])
    service, index, embedder = make(store)
    service.build()
    assert index.keys().get(PAPER_C)
    calls = embedder.calls
    service.build()
    assert embedder.calls == calls


def test_status_counts_what_a_build_would_do(store):
    service, index, _ = make(store)
    before = service.status()
    assert (before.new, before.changed, before.unchanged, before.stale) == (2, 0, 0, [])
    assert before.chunks_to_embed > 0 and before.stats.documents == 0 and before.embedder_matches
    service.build()
    add_doc(store, PAPER_A, [("Changed text about graphs and spectra of graphs.", "Intro")])
    add_doc(store, PAPER_C, [("A new paper on the Laplacian of a graph.", "Intro")])
    remove_doc(store, PAPER_B)
    after = service.status()
    assert (after.new, after.changed, after.unchanged) == (1, 1, 0)
    assert after.stale == [PAPER_B] and after.corpus_documents == 2
    assert after.stats.searchable_documents == 2
    assert after.index_fingerprint == index.fingerprint


def test_status_reports_an_embedder_mismatch_instead_of_failing(store):
    service, _, _ = make(
        store, index=FakeIndex(embedder_fingerprint="old"), embedder=FakeEmbedder(fingerprint="new")
    )
    assert service.status().embedder_matches is False


def test_remove_forgets_a_document_by_id_or_unique_prefix_and_commits(store):
    service, index, embedder = make(store)
    service.build()
    assert service.remove(PAPER_A[:12]) == PAPER_A
    assert set(index.keys()) == {PAPER_B}
    hits = index.search("softmax", embedder.embed_query("softmax"), 100)
    assert {h.chunk.doc_id for h in hits} == {PAPER_B}
    assert service.remove(PAPER_B) == PAPER_B
    assert index.keys() == {}


@pytest.mark.parametrize(
    ("ask", "why"),
    [("f" * 20, "not in the index"), ("a" * 4, "not in the index"), ("", "not in the index")],
)
def test_remove_refuses_unknown_or_too_short_ids(store, ask, why):
    service, index, _ = make(store)
    service.build()
    with pytest.raises(ValueError, match=why):
        service.remove(ask)
    assert len(index.keys()) == 2


def test_remove_refuses_an_ambiguous_prefix(store):
    add_doc(store, "a" * 20 + "1" * 44, [("one", "x")])
    service, index, _ = make(store)
    service.build()
    with pytest.raises(ValueError, match="ambiguous"):
        service.remove("a" * 10)
    assert len(index.keys()) == 3


class FailingEmbedder(FakeEmbedder):
    """Embeds ``ok`` calls, then fails like a server that went away."""

    def __init__(self, ok: int):
        super().__init__()
        self.ok = ok

    def embed_documents(self, texts):
        if self.calls >= self.ok:
            raise ConnectionError("embedding server went away")
        return super().embed_documents(texts)


def test_the_index_is_closed_when_a_build_fails_between_two_batches(store):
    embedder = FailingEmbedder(ok=1)
    service, index, _ = make(store, embedder=embedder, batch_chunks=3)
    with pytest.raises(ConnectionError):
        service.build()
    assert index.closed == 1
    assert index.keys() and index.stats().searchable_documents == 0  # stored, never committed


def test_a_build_that_works_closes_the_index_and_so_do_status_remove_and_update(store):
    service, index, _ = make(store)
    service.build()
    assert index.closed == 1
    service.status()
    assert index.closed == 2
    service.remove(PAPER_A)
    assert index.closed == 3
    with pytest.raises(ValueError, match="not in the index"):
        service.remove(PAPER_A)
    assert index.closed == 4
    service.update(store.corpus()[0])
    assert index.closed == 5


def test_a_mismatch_stops_before_the_index_is_touched(store):
    service, index, _ = make(
        store, index=FakeIndex(embedder_fingerprint="old"), embedder=FakeEmbedder(fingerprint="new")
    )
    with pytest.raises(IndexMismatchError):
        service.update(store.corpus()[0])
    with pytest.raises(IndexMismatchError):
        service.check_embedder()
    assert index.closed == 0


def test_update_indexes_only_the_given_documents_and_prunes_nothing(store):
    service, index, embedder = make(store)
    service.build()
    add_doc(store, PAPER_C, [("Graph spectra and the Laplacian of a graph.", "Intro")])
    remove_doc(store, PAPER_A)  # gone from the corpus: update must not prune it
    [new] = [d for d in store.corpus()[0] if d.manifest.doc_id == PAPER_C]
    reads = []
    original = store.markdown
    store.markdown = lambda doc: (reads.append(doc.manifest.doc_id), original(doc))[1]
    report = service.update([new])
    assert (report.added, report.updated, report.unchanged, report.pruned) == (1, 0, 0, [])
    assert report.committed and reads == [PAPER_C]  # no other document was read
    assert set(index.keys()) == {PAPER_A, PAPER_B, PAPER_C}
    question = "Graph spectra and the Laplacian of a graph."
    assert index.search(question, embedder.embed_query(question), 1)[0].chunk.doc_id == PAPER_C
    again = service.update([new])
    assert (again.added, again.unchanged, again.committed) == (0, 1, False)


def test_update_indexes_the_version_the_corpus_serves_not_the_variant_it_was_given(store):
    service, index, _ = make(store)
    service.build()
    canonical = index.keys()[PAPER_B]
    [doc] = [d for d in store.corpus()[0] if d.manifest.doc_id == PAPER_B]
    first = doc.manifest.model_copy(
        update={"n_pages": 1, "max_pages": 1, "pages": doc.manifest.pages[:1]}
    )
    partial = store.save(first, render_markdown(first, [TEXT_B[0][0]]))  # a --max-pages 1 run
    assert partial.canonical is False
    assert [d.manifest.n_pages for d in store.corpus()[0] if d.manifest.doc_id == PAPER_B] == [2]
    report = service.update([partial])
    assert (report.added, report.updated, report.unchanged, report.committed) == (0, 0, 1, False)
    assert index.keys()[PAPER_B] == canonical  # still the two-page paper
    assert service.status().changed == 0


def test_a_first_result_that_is_only_a_variant_is_indexed_because_the_corpus_serves_it(store):
    service, index, _ = make(store)
    only = InMemoryStore()
    add_doc(only, PAPER_C, [("Graph spectra.", "Intro"), ("More about graphs.", "Body")])
    [full] = only.corpus()[0]
    first = full.manifest.model_copy(
        update={"n_pages": 1, "max_pages": 1, "pages": full.manifest.pages[:1]}
    )
    del only.canonical[PAPER_C]
    partial = only.save(first, render_markdown(first, ["Graph spectra."]))
    service.store = only
    report = service.update([partial])
    assert report.added == 1 and set(index.keys()) == {PAPER_C}


def test_update_ignores_a_document_the_corpus_does_not_serve(store):
    service, index, _ = make(store)
    [doc] = [d for d in store.corpus()[0] if d.manifest.doc_id == PAPER_A]
    del store.canonical[PAPER_A]
    report = service.update([doc])
    assert (report.added, report.unchanged) == (0, 0) and index.keys() == {}


def test_update_with_nothing_to_index_is_fine(store):
    service, index, embedder = make(store)
    report = service.update([])
    assert report.committed and embedder.calls == 0 and index.stats().committed_at


def test_the_key_depends_on_page_numbers_and_texts_only():
    pages = {1: "one", 2: "two"}
    assert content_key(pages) == content_key(dict(pages))
    assert content_key(pages) != content_key({1: "one", 3: "two"})
    assert content_key(pages) != content_key({1: "onetwo"})
    assert content_key({1: "ab", 2: "c"}) != content_key({1: "a", 2: "bc"})
