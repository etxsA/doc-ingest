"""AskService with fakes: the two-stage retrieval, the plain path and the errors."""

import asyncio

import pytest
from fakes import FakeEmbedder, FakeIndex, FakeQA, FakeReranker, InMemoryStore, add_doc

from docingest.application.ask import AskService, Retrieval
from docingest.application.index import IndexService
from docingest.domain.errors import (
    DocingestError,
    IndexMismatchError,
    IndexNotReadyError,
    RetrievalError,
)

CHARS, OVERLAP = 60, 10
PAPER_A, PAPER_B, PAPER_C = "a" * 64, "b" * 64, "c" * 64
TEXT_A = [("Attention weights use a softmax over scaled dot products. " * 4, "Method")]
TEXT_B = [("Qubits decohere when they couple to the environment. " * 4, "Intro")]
TEXT_C = [("Graph spectra and the Laplacian of a graph. " * 4, "Intro")]


class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.queries: list[str] = []

    def embed_query(self, text):
        self.queries.append(text)
        return super().embed_query(text)


class ScriptedReranker:
    """Scores chunks by a function of their text; records what it was asked."""

    def __init__(self, score):
        self.score = score
        self.asked: list[tuple[str, list[str]]] = []

    def rerank(self, question, chunks):
        self.asked.append((question, [c.text for c in chunks]))
        return [self.score(c.text) for c in chunks]


@pytest.fixture
def store():
    s = InMemoryStore()
    add_doc(s, PAPER_A, TEXT_A, name="a.txt")
    add_doc(s, PAPER_B, TEXT_B, name="b.txt")
    add_doc(s, PAPER_C, TEXT_C, name="c.txt")
    return s


def build_index(store, embedder=None):
    embedder = embedder or CountingEmbedder()
    index = FakeIndex(embedder_fingerprint=embedder.fingerprint)
    IndexService(
        store=store,
        embedder=embedder,
        index=index,
        chunk_chars=CHARS,
        overlap=OVERLAP,
        log=lambda _: None,
    ).build()
    index.closed = 0
    return embedder, index


def service(store, qa, embedder, index, reranker=None, *, candidates=50, contexts=10):
    retrieval = Retrieval(embedder, index, reranker, candidates, contexts)
    return AskService(store=store, qa=qa, retrieval=lambda: retrieval)


def ask(svc, question="how do qubits decohere in the environment", **kw):
    warnings: list[str] = []
    answer = asyncio.run(svc.ask(question, warnings.append, **kw))
    return answer, warnings


# ------------------------------------------------------------------ no index: as before


def test_without_an_index_the_whole_corpus_goes_to_the_answerer_as_today(store):
    qa = FakeQA()
    svc = AskService(store=store, qa=qa)
    answer, warnings = ask(svc)
    assert "3 docs" in answer and warnings == [] and svc.has_index is False
    assert sorted(qa.seen) == ["a.txt", "b.txt", "c.txt"] and qa.contexts is None
    assert all(md for md in qa.markdown)  # the Markdown is there: PaperQA2 retrieves by itself


def test_use_index_false_skips_a_configured_index_and_never_builds_it(store):
    qa = FakeQA()
    built = []
    svc = AskService(store=store, qa=qa, retrieval=lambda: built.append(1))  # type: ignore[arg-type,return-value]
    assert svc.has_index is True
    ask(svc, use_index=False)
    assert built == [] and qa.contexts is None and len(qa.seen) == 3


def test_keywords_mean_nothing_without_the_index(store):
    with pytest.raises(ValueError, match="keywords applies to the index"):
        ask(AskService(store=store, qa=FakeQA()), keywords="never")
    with pytest.raises(ValueError, match="keywords"):
        ask(
            AskService(store=store, qa=FakeQA(), retrieval=lambda: None),  # type: ignore[arg-type,return-value]
            use_index=False,
            keywords="never",
        )


def test_a_corpus_without_documents_is_an_error_with_or_without_an_index(store):
    empty = InMemoryStore()
    embedder, index = build_index(store)
    for svc in (
        AskService(store=empty, qa=FakeQA()),
        service(empty, FakeQA(), embedder, index),
    ):
        with pytest.raises(RuntimeError, match="no ingested documents"):
            ask(svc)


# ------------------------------------------------------------------ the index path


def test_the_question_is_embedded_once_and_the_answerer_gets_only_the_retrieved_chunks(store):
    embedder, index = build_index(store)
    qa = FakeQA()
    question = "how do qubits decohere in the environment"
    answer, warnings = ask(service(store, qa, embedder, index, contexts=3), question)
    assert embedder.queries == [question]
    assert qa.contexts is not None and len(qa.contexts) == 3
    assert all(c.doc_id == PAPER_B for c in qa.contexts)
    assert qa.seen == ["b.txt"] and qa.markdown == [""]  # only the paper of the chunks, no text
    assert "from 1 docs" in answer and warnings == []
    assert index.closed == 1  # the index is closed once the question is answered


def test_contexts_come_from_several_papers_in_order_of_first_appearance(store):
    embedder, index = build_index(store)
    qa = FakeQA()
    ask(service(store, qa, embedder, index, contexts=30), "attention softmax qubits graph")
    assert qa.contexts is not None
    order = list(dict.fromkeys(c.doc_id for c in qa.contexts))
    names = {PAPER_A: "a.txt", PAPER_B: "b.txt", PAPER_C: "c.txt"}
    assert qa.seen == [names[d] for d in order] and len(order) == 3


def test_the_first_stage_is_asked_for_candidates_and_the_reranker_sees_all_of_them(store):
    embedder, index = build_index(store)
    total = index.stats().chunks
    assert total > 6
    seen_k = []
    original = index.search
    index.search = lambda q, v, k, **kw: (seen_k.append(k), original(q, v, k, **kw))[1]
    reranker = ScriptedReranker(lambda text: 0.0)
    ask(service(store, FakeQA(), embedder, index, reranker, candidates=6, contexts=2))
    assert seen_k == [6]
    [(_, texts)] = reranker.asked
    assert len(texts) == 6


def test_the_reranker_decides_the_final_order_and_the_cut(store):
    embedder, index = build_index(store)
    # first stage: chunks about qubits come first; the reranker prefers the attention paper
    reranker = ScriptedReranker(lambda text: 1.0 if "softmax" in text else 0.0)
    qa = FakeQA()
    ask(
        service(store, qa, embedder, index, reranker, candidates=50, contexts=2),
        "how do qubits decohere in the environment",
    )
    assert qa.contexts is not None and len(qa.contexts) == 2
    assert all("softmax" in c.text for c in qa.contexts)
    assert qa.seen == ["a.txt"]


def test_equal_reranker_scores_keep_the_first_stage_order(store):
    embedder, index = build_index(store)
    question = "how do qubits decohere in the environment"
    first_stage = [h.chunk for h in index.search(question, embedder.embed_query(question), 8)]
    qa = FakeQA()
    reranker = ScriptedReranker(lambda text: 0.5)  # a tie everywhere
    ask(service(store, qa, embedder, index, reranker, candidates=8, contexts=5), question)
    assert qa.contexts == first_stage[:5]


def test_a_cross_encoder_score_can_promote_a_chunk_the_dense_search_ranked_lower(store):
    embedder, index = build_index(store)
    qa = FakeQA()
    ask(
        service(store, qa, embedder, index, FakeReranker(), candidates=50, contexts=1),
        "graph spectra Laplacian",
    )
    assert qa.contexts is not None and qa.contexts[0].doc_id == PAPER_C


def test_without_a_reranker_the_first_stage_top_contexts_are_taken_as_they_are(store):
    embedder, index = build_index(store)
    question = "how do qubits decohere in the environment"
    first_stage = [h.chunk for h in index.search(question, embedder.embed_query(question), 50)]
    qa = FakeQA()
    ask(service(store, qa, embedder, index, None, candidates=50, contexts=4), question)
    assert qa.contexts == first_stage[:4]


def test_a_reranker_that_scores_the_wrong_number_of_chunks_is_a_retrieval_error(store):
    embedder, index = build_index(store)

    class ShortReranker:
        def rerank(self, question, chunks):
            return [0.0]  # one score for many chunks

    qa = FakeQA()
    with pytest.raises(RetrievalError, match="1 scores for"):
        ask(service(store, qa, embedder, index, ShortReranker()))
    assert qa.contexts is None and issubclass(RetrievalError, DocingestError)


def test_fewer_candidates_than_contexts_gives_what_there_is(store):
    embedder, index = build_index(store)
    qa = FakeQA()
    ask(service(store, qa, embedder, index, None, candidates=2, contexts=10))
    assert qa.contexts is not None and len(qa.contexts) == 2


def test_the_keyword_choice_reaches_the_index(store):
    embedder, index = build_index(store)
    ask(service(store, FakeQA(), embedder, index), keywords="never")
    ask(service(store, FakeQA(), embedder, index))
    assert index.keywords == ["never", None]


def test_a_retrieval_needs_at_least_one_candidate_and_one_context():
    for kw in ({"candidates": 0, "contexts": 1}, {"candidates": 1, "contexts": 0}):
        with pytest.raises(ValueError, match="at least 1"):
            Retrieval(FakeEmbedder(), FakeIndex(), None, **kw)


# ------------------------------------------------------------------ what the index cannot answer


def test_an_empty_index_says_to_build_it_and_the_answerer_is_not_called(store):
    embedder = CountingEmbedder()
    index = FakeIndex(embedder_fingerprint=embedder.fingerprint)
    qa = FakeQA()
    with pytest.raises(IndexNotReadyError, match=r"index is empty.*docingest index build"):
        ask(service(store, qa, embedder, index))
    assert qa.contexts is None and qa.seen == [] and embedder.queries == []
    assert index.closed == 1


def test_documents_stored_but_never_committed_are_not_searchable_and_the_message_says_so(store):
    embedder = CountingEmbedder()
    index = FakeIndex(embedder_fingerprint=embedder.fingerprint)
    pages = {1: "Qubits decohere."}
    from docingest.domain.chunking import chunk_pages

    chunks = chunk_pages(PAPER_B, pages, chunk_chars=CHARS, overlap=OVERLAP)
    index.upsert(PAPER_B, "k", chunks, embedder.embed_documents([c.text for c in chunks]))
    assert index.stats().pending and index.stats().searchable_documents == 0
    with pytest.raises(IndexNotReadyError, match=r"none is committed.*docingest index build"):
        ask(service(store, FakeQA(), embedder, index))


def test_an_index_built_with_another_embedder_is_refused_before_the_question_is_embedded(store):
    _, index = build_index(store)
    other = CountingEmbedder()
    other.fingerprint = "another-model 7"
    with pytest.raises(IndexMismatchError, match=r"another-model 7.*docingest index build"):
        ask(service(store, FakeQA(), other, index))
    assert other.queries == []


def test_the_index_is_closed_when_the_embedder_fails(store):
    embedder, index = build_index(store)

    def down(text):
        raise ConnectionError("embedding server went away")

    embedder.embed_query = down
    with pytest.raises(ConnectionError):
        ask(service(store, FakeQA(), embedder, index))
    assert index.closed == 1


def test_documents_missing_from_the_index_are_reported_but_the_rest_still_answers(store):
    embedder, index = build_index(store)
    add_doc(store, "d" * 64, [("A paper added after the build.", "Intro")], name="d.txt")
    qa = FakeQA()
    _, warnings = ask(service(store, qa, embedder, index))
    assert qa.contexts and any(
        "1 of 4 documents" in w and "docingest index build" in w for w in warnings
    )


def test_uncommitted_changes_are_reported(store):
    embedder, index = build_index(store)
    index.remove(PAPER_C)  # staged, not committed
    _, warnings = ask(service(store, FakeQA(), embedder, index))
    assert any("not committed" in w and "docingest index build" in w for w in warnings)


def test_chunks_of_papers_that_left_the_corpus_are_left_out_with_a_warning(store):
    embedder, index = build_index(store)
    del store.canonical[PAPER_B]
    qa = FakeQA()
    _, warnings = ask(
        service(store, qa, embedder, index, contexts=50), "qubits decohere environment"
    )
    assert qa.contexts and PAPER_B not in {c.doc_id for c in qa.contexts}
    assert any("no longer in the corpus" in w for w in warnings)


def test_an_index_that_finds_nothing_does_not_suggest_a_rebuild(store):
    embedder, index = build_index(store)
    index.search = lambda *args, **kw: []  # committed papers, but nothing to return
    qa = FakeQA()
    with pytest.raises(RetrievalError, match="no passage") as info:
        ask(service(store, qa, embedder, index))
    assert "index build" not in str(info.value) and qa.contexts is None


def test_when_every_retrieved_chunk_left_the_corpus_it_is_an_error_not_an_empty_answer(store):
    embedder, index = build_index(store)
    gone = InMemoryStore()
    add_doc(gone, "e" * 64, [("An unrelated paper.", "Intro")], name="e.txt")
    qa = FakeQA()
    with pytest.raises(IndexNotReadyError, match="left the corpus"):
        ask(service(gone, qa, embedder, index))
    assert qa.contexts is None
