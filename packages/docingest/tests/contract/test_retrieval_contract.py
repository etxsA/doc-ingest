"""Embedder, ChunkIndex and Reranker contracts: every implementation must pass the same tests.

Each implementation supplies the fixtures ``make_embedder`` (a factory that takes the
``query_instruction``), ``index`` and ``reranker``. The ``index`` must be empty and built for
the embedder that ``make_embedder()`` returns. The ``docingest-index`` package imports this
module and overrides the fixtures with its real adapters.
"""

from collections.abc import Callable
from dataclasses import replace

import pytest
from fakes import FakeEmbedder, FakeIndex, FakeReranker

from docingest.domain.chunking import chunk_pages
from docingest.ports import ChunkIndex, Embedder, Reranker

PAPER_A = "a" * 64
PAPER_B = "b" * 64
TEXTS = {
    PAPER_A: {1: "Attention weights are computed with a softmax over scaled dot products."},
    PAPER_B: {
        1: "Qubits decohere when they couple to the environment.",
        2: "Le refroidissement réduit la décohérence.",
    },
}


@pytest.fixture(params=["fake"])
def make_embedder(request) -> Callable[..., Embedder]:
    def make(query_instruction: str = "") -> Embedder:
        impl = FakeEmbedder(query_instruction=query_instruction)
        assert isinstance(impl, Embedder)
        return impl

    return make


@pytest.fixture
def embedder(make_embedder) -> Embedder:
    return make_embedder()


@pytest.fixture(params=["fake"])
def index(request) -> ChunkIndex:
    impl = FakeIndex()
    assert isinstance(impl, ChunkIndex)
    return impl


@pytest.fixture(params=["fake"])
def reranker(request) -> Reranker:
    impl = FakeReranker()
    assert isinstance(impl, Reranker)
    return impl


def chunks_of(doc_id):
    chunks = chunk_pages(doc_id, TEXTS[doc_id], chunk_chars=60, overlap=10)
    if doc_id == PAPER_B:  # every field must survive the index, not only the ones set by default
        chunks[-1] = replace(chunks[-1], is_reference=True)
    return chunks


def fill(index, embedder, key="k1"):
    for doc_id in TEXTS:
        chunks = chunks_of(doc_id)
        index.upsert(doc_id, key, chunks, embedder.embed_documents([c.text for c in chunks]))
    index.commit()


def test_the_embedder_returns_one_vector_per_text_in_order(embedder):
    texts = ["softmax attention", "qubit decoherence", "softmax attention"]
    vectors = embedder.embed_documents(texts)
    assert len(vectors) == 3 and len({len(v) for v in vectors}) == 1
    assert vectors[0] == vectors[2] and vectors[0] != vectors[1]
    assert embedder.embed_documents([]) == []


def test_the_query_vector_has_the_dimension_of_the_document_vectors(embedder):
    [doc] = embedder.embed_documents(["softmax attention"])
    assert len(embedder.embed_query("softmax attention")) == len(doc)


def test_the_embedder_names_its_settings(embedder):
    assert isinstance(embedder.fingerprint, str) and embedder.fingerprint
    assert isinstance(embedder.query_instruction, str)


def test_the_query_instruction_is_not_part_of_the_document_fingerprint(make_embedder):
    plain, instructed = make_embedder(), make_embedder("find the passage that answers it")
    assert instructed.query_instruction == "find the passage that answers it"
    assert plain.fingerprint == instructed.fingerprint
    assert plain.embed_documents(["x y"]) == instructed.embed_documents(["x y"])


def test_the_index_starts_empty_and_keeps_the_key_of_each_document(index, embedder):
    assert index.keys() == {} and isinstance(index.fingerprint, str)
    fill(index, embedder)
    assert index.keys() == {PAPER_A: "k1", PAPER_B: "k1"}


def test_the_index_names_the_embedder_it_was_built_for(index, embedder):
    assert index.embedder_fingerprint == embedder.fingerprint


def test_stats_count_what_is_stored_and_what_is_searchable(index, embedder):
    empty = index.stats()
    assert (empty.documents, empty.chunks, empty.searchable_documents) == (0, 0, 0)
    assert empty.searchable_chunks == 0 and empty.committed_at is None
    chunks = chunks_of(PAPER_A)
    index.upsert(PAPER_A, "k1", chunks, embedder.embed_documents([c.text for c in chunks]))
    staged = index.stats()
    assert (staged.documents, staged.chunks) == (1, len(chunks))
    assert (staged.searchable_documents, staged.searchable_chunks) == (0, 0)
    index.commit()
    done = index.stats()
    assert (done.searchable_documents, done.searchable_chunks) == (1, len(chunks))
    assert isinstance(done.committed_at, str) and done.committed_at
    index.remove(PAPER_A)
    assert (index.stats().documents, index.stats().searchable_documents) == (0, 1)


def test_search_finds_the_matching_chunk_first(index, embedder):
    fill(index, embedder)
    question = "how do qubits decohere in the environment"
    hits = index.search(question, embedder.embed_query(question), 3)
    assert hits[0].chunk.doc_id == PAPER_B and "Qubits" in hits[0].chunk.text
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_search_returns_at_most_k_hits(index, embedder):
    fill(index, embedder)
    total = sum(len(chunks_of(d)) for d in TEXTS)
    assert total > 2
    assert len(index.search("anything", embedder.embed_query("anything"), 2)) == 2
    assert len(index.search("anything", embedder.embed_query("anything"), 100)) == total
    assert index.search("anything", embedder.embed_query("anything"), 0) == []


def test_chunks_come_back_unchanged(index, embedder):
    fill(index, embedder)
    everything = {c for d in TEXTS for c in chunks_of(d)}
    hits = index.search("anything", embedder.embed_query("anything"), 100)
    assert {h.chunk for h in hits} == everything
    assert any(h.chunk.is_reference for h in hits) and any("é" in h.chunk.text for h in hits)
    assert any(h.chunk.start > 0 for h in hits)


@pytest.mark.parametrize("keywords", [None, "english", "always", "never"])
def test_search_takes_the_keyword_mode_whatever_the_index_does_with_it(index, embedder, keywords):
    fill(index, embedder)
    question = "how do qubits decohere in the environment"
    hits = index.search(question, embedder.embed_query(question), 2, keywords=keywords)
    assert 0 < len(hits) <= 2  # an index without a keyword part ignores the choice


def test_upsert_needs_one_vector_per_chunk(index, embedder):
    chunks = chunks_of(PAPER_A)
    with pytest.raises(ValueError, match="vector"):
        index.upsert(PAPER_A, "k1", chunks, embedder.embed_documents(["only one"]))


def test_changes_reach_search_only_at_commit_but_keys_see_them_at_once(index, embedder):
    chunks = chunks_of(PAPER_A)
    vectors = embedder.embed_documents([c.text for c in chunks])
    index.upsert(PAPER_A, "k1", chunks, vectors)
    query = embedder.embed_query("softmax")
    assert index.keys() == {PAPER_A: "k1"}
    assert index.search("softmax", query, 10) == []
    index.commit()
    assert len(index.search("softmax", query, 10)) == len(chunks)
    index.remove(PAPER_A)
    assert index.keys() == {}
    assert len(index.search("softmax", query, 10)) == len(chunks)
    index.commit()
    assert index.search("softmax", query, 10) == []


def test_search_answers_from_the_last_commit_whatever_happens_to_the_index_after_it(
    index, embedder
):
    """No search before the change: nothing may be cached from it. Each case reads the
    committed chunks only, so the old text of a document stays with its old vector."""
    fill(index, embedder)
    committed = {c for d in TEXTS for c in chunks_of(d)}
    query = embedder.embed_query("softmax attention qubits")

    def searched():
        return {h.chunk for h in index.search("anything", query, 100)}

    new = chunk_pages(PAPER_A, {1: "Replaced text about graphs."}, chunk_chars=60, overlap=10)
    vectors = embedder.embed_documents([c.text for c in new])
    index.upsert(PAPER_A, "k2", new, vectors)  # another key
    assert searched() == committed
    index.upsert(PAPER_A, "k1", new, vectors)  # the same key, other chunks
    assert searched() == committed
    index.remove(PAPER_B)
    assert searched() == committed
    index.commit()
    assert searched() == set(new)


def test_a_removal_committed_and_then_searched_for_the_first_time(index, embedder):
    fill(index, embedder)
    index.remove(PAPER_A)
    index.commit()
    hits = index.search("anything", embedder.embed_query("attention"), 100)
    assert {h.chunk.doc_id for h in hits} == {PAPER_B}


def test_close_is_safe_at_any_time_and_the_index_stays_usable(index, embedder):
    index.close()  # nothing opened yet
    fill(index, embedder)
    index.close()
    index.close()
    query = embedder.embed_query("softmax")
    assert len(index.search("softmax", query, 100)) == sum(len(chunks_of(d)) for d in TEXTS)
    chunks = chunks_of(PAPER_A)
    index.upsert(PAPER_A, "k2", chunks, embedder.embed_documents([c.text for c in chunks]))
    index.close()  # writes that are not committed are not lost by closing
    assert index.keys()[PAPER_A] == "k2"
    index.commit()
    index.close()
    assert index.stats().pending is False


def test_pending_tells_changes_that_counts_cannot(index, embedder):
    assert index.stats().pending is False
    fill(index, embedder)
    assert index.stats().pending is False
    new = chunk_pages(PAPER_A, {1: "Replaced text about graphs."}, chunk_chars=60, overlap=10)
    index.upsert(PAPER_A, "k2", new, embedder.embed_documents([c.text for c in new]))
    stats = index.stats()  # one document replaced by one: the counts of documents are equal
    assert stats.documents == stats.searchable_documents and stats.pending is True
    index.commit()
    assert index.stats().pending is False
    index.remove(PAPER_A)
    assert index.stats().pending is True
    index.commit()
    assert index.stats().pending is False


def test_begin_write_is_reentrant_and_released_by_close_and_commit(index, embedder):
    index.begin_write()
    index.begin_write()  # this object already holds the lock: a no-op
    fill(index, embedder)  # upserts and a commit under the lock taken first
    assert index.stats().searchable_documents == 2
    index.begin_write()
    index.close()  # giving up releases it
    index.begin_write()
    index.commit()  # a commit releases it too
    index.begin_write()
    index.close()
    assert index.stats().searchable_documents == 2


def test_committed_agrees_with_stats_and_keys_in_every_state(index, embedder):
    def check(documents, pending):
        state, stats = index.committed(), index.stats()
        assert state.documents == documents and state.pending is pending
        assert len(state.documents) == stats.searchable_documents and stats.pending is pending
        if not pending:
            assert state.documents == set(index.keys())

    check(set(), pending=False)  # nothing stored
    chunks = chunks_of(PAPER_A)
    index.upsert(PAPER_A, "k1", chunks, embedder.embed_documents([c.text for c in chunks]))
    check(set(), pending=True)  # stored, never committed
    index.commit()
    check({PAPER_A}, pending=False)
    chunks = chunks_of(PAPER_B)
    index.upsert(PAPER_B, "k1", chunks, embedder.embed_documents([c.text for c in chunks]))
    check({PAPER_A}, pending=True)  # a document added since the commit is not searched yet
    index.commit()
    check({PAPER_A, PAPER_B}, pending=False)
    new = chunk_pages(PAPER_A, {1: "Replaced text about graphs."}, chunk_chars=60, overlap=10)
    index.upsert(PAPER_A, "k2", new, embedder.embed_documents([c.text for c in new]))
    check({PAPER_A, PAPER_B}, pending=True)  # replaced: the counts of documents are equal
    index.commit()
    check({PAPER_A, PAPER_B}, pending=False)
    index.remove(PAPER_B)
    check({PAPER_A, PAPER_B}, pending=True)  # still searchable until the commit
    index.commit()
    index.remove(PAPER_A)
    index.commit()
    check(set(), pending=False)  # committed empty


def test_upsert_replaces_everything_indexed_for_the_document(index, embedder):
    fill(index, embedder)
    new = chunk_pages(PAPER_A, {1: "Replaced text about graphs."}, chunk_chars=60, overlap=10)
    index.upsert(PAPER_A, "k2", new, embedder.embed_documents([c.text for c in new]))
    index.commit()
    assert index.keys() == {PAPER_A: "k2", PAPER_B: "k1"}
    hits = index.search("softmax", embedder.embed_query("softmax"), 100)
    assert not any("softmax" in h.chunk.text for h in hits)
    assert any("Replaced" in h.chunk.text for h in hits)


def test_remove_forgets_a_document_and_ignores_unknown_ones(index, embedder):
    fill(index, embedder)
    index.remove(PAPER_A)
    index.remove("unknown")
    index.commit()
    assert index.keys() == {PAPER_B: "k1"}
    hits = index.search("attention", embedder.embed_query("attention"), 100)
    assert {h.chunk.doc_id for h in hits} == {PAPER_B}


def test_the_reranker_scores_each_chunk_in_the_order_given(reranker):
    chunks = chunks_of(PAPER_A) + chunks_of(PAPER_B)
    scores = reranker.rerank("softmax attention weights", chunks)
    assert len(scores) == len(chunks)
    best = max(range(len(chunks)), key=scores.__getitem__)
    assert chunks[best].doc_id == PAPER_A
    assert reranker.rerank("softmax", []) == []
