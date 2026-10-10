import asyncio

import pytest

pytest.importorskip("paperqa")

import litellm
from paperqa.docs import Docs

from docingest.adapters.qa import paperqa
from docingest.adapters.qa.paperqa import PaperQAAnswerer, embedding_path, token_windows
from docingest.config import AppConfig
from docingest.domain.chunking import CHUNK_CHARS, OVERLAP, chunk_pages
from docingest.domain.models import DocumentManifest, SourceKind
from docingest.ports import StoredDocument


def test_token_windows_cover_long_chunks_within_limit():
    from paperqa.types import Doc, Text
    from transformers import AutoTokenizer

    try:
        tok = AutoTokenizer.from_pretrained(embedding_path(AppConfig()))
    except Exception:
        pytest.skip("embedder not in local HF cache")
    doc = Doc(docname="d", dockey="d", citation="c")
    long = " ".join(f"word{i}" for i in range(600))
    out = token_windows([Text(text=long, name="d pages 1-1", doc=doc)], tok, 254)
    assert len(out) > 1 and all(t.name == "d pages 1-1" for t in out)
    assert all(len(tok(t.text, add_special_tokens=False)["input_ids"]) <= 254 for t in out)
    assert out[0].text.startswith("word0") and out[-1].text.endswith("word599")


# ------------------------------------------------ answering from given contexts

REPLY = "Attention is a softmax over scaled dot products.\n\nRelevance Score: 9"


@pytest.fixture
def mock_llm(monkeypatch):
    """litellm answers every request with a canned reply: no server, no network."""
    real = paperqa.llm_params
    monkeypatch.setattr(paperqa, "llm_params", lambda cfg: {**real(cfg), "mock_response": REPLY})


@pytest.fixture
def qa_cfg():
    # "sparse" is PaperQA2's keyword embedder: nothing to download. A custom embedding is
    # also what keeps the pinned embedder's tokenizer out of these tests.
    return AppConfig.model_validate({"qa": {"llm": "openai/mock", "embedding": "sparse"}})


def chunks_of(doc_id, pages):
    return chunk_pages(doc_id, pages, chunk_chars=CHUNK_CHARS, overlap=OVERLAP)


def stored(letter: str, n_pages: int = 2, source_pages: int = 2) -> StoredDocument:
    manifest = DocumentManifest(
        doc_id=letter * 64,
        source_path="x",
        source_name=f"{letter}.pdf",
        source_kind=SourceKind.PDF,
        mime="application/pdf",
        size_bytes=1,
        n_pages=n_pages,
        source_pages=source_pages,
        max_pages=n_pages if n_pages < source_pages else None,
        pages=[],
        pipeline_version="t",
        config_hash="h",
        title=f"Paper {letter}",
    )
    return StoredDocument(manifest, f"mem://{letter}", canonical=n_pages == source_pages)


@pytest.fixture
def spy_evidence(monkeypatch):
    """Records what the Docs holds and whether retrieval is on when evidence is gathered."""
    seen = []
    original = Docs.aget_evidence

    async def spy(self, query, *args, **kwargs):
        settings = kwargs["settings"]
        seen.append(
            (list(self.texts), list(self.docs.values()), settings.answer.evidence_retrieval)
        )
        return await original(self, query, *args, **kwargs)

    monkeypatch.setattr(Docs, "aget_evidence", spy)
    return seen


@pytest.fixture
def llm_calls(monkeypatch):
    """The keyword arguments of every request litellm is asked to send."""
    sent = []
    original = litellm.acompletion

    async def spy(*args, **kwargs):
        sent.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(litellm, "acompletion", spy)
    return sent


def test_given_contexts_are_the_only_evidence(mock_llm, qa_cfg, spy_evidence):
    a, b = stored("a"), stored("b", n_pages=1, source_pages=3)
    contexts = [
        *chunks_of(a.manifest.doc_id, {1: "Attention is a softmax over scaled dot products."}),
        *chunks_of(b.manifest.doc_id, {1: "A second paper."}),
        *chunks_of(a.manifest.doc_id, {2: "More on attention heads."}),
    ]
    # The Markdown is not read: only the manifests are needed, and papers may be in any order.
    documents = [(b, ""), (a, "")]
    answer = asyncio.run(
        PaperQAAnswerer(qa_cfg).ask("What is attention?", documents, print, contexts=contexts)
    )
    [(texts, docs, retrieval)] = spy_evidence
    assert retrieval is False
    # Added per paper in order of first appearance, each chunk as given.
    assert [(t.name, t.text) for t in texts] == [
        (contexts[0].name, contexts[0].text),
        (contexts[2].name, contexts[2].text),
        (contexts[1].name, contexts[1].text),
    ]
    assert [d.dockey for d in docs] == [a.manifest.doc_id, b.manifest.doc_id]
    assert "Paper a" in docs[0].citation
    assert docs[1].citation.endswith(", pages 1-1 of 3")  # a partial run says so
    assert answer.startswith("Question: What is attention?")


@pytest.mark.parametrize("temperature", [0.0, 0.4])
def test_every_request_carries_the_temperature(mock_llm, llm_calls, spy_evidence, temperature):
    cfg = AppConfig.model_validate(
        {"qa": {"llm": "openai/mock", "embedding": "sparse", "temperature": temperature}}
    )
    a = stored("a")
    contexts = [
        *chunks_of(a.manifest.doc_id, {1: "Attention is a softmax."}),
        *chunks_of(a.manifest.doc_id, {2: "Heads run in parallel."}),
    ]
    asyncio.run(PaperQAAnswerer(cfg).ask("What is attention?", [(a, "")], print, contexts=contexts))
    # one summary per context, then the answer
    assert len(llm_calls) == len(contexts) + 1
    assert {call["temperature"] for call in llm_calls} == {temperature}


@pytest.fixture
def pinned_embedder(monkeypatch):
    """The default config (the pinned MiniLM); every construction of an embedder is counted
    and returns a sparse one, so nothing is downloaded or loaded."""
    from paperqa import Settings, SparseEmbeddingModel

    built = []

    def get_embedding_model(self):
        built.append(self.embedding)
        return SparseEmbeddingModel()

    monkeypatch.setattr(Settings, "get_embedding_model", get_embedding_model)
    monkeypatch.setattr(paperqa, "embedding_path", lambda cfg: "pinned-minilm")
    monkeypatch.setattr(PaperQAAnswerer, "_splitter", lambda self: None)  # needs the tokenizer
    return built


def test_given_contexts_never_build_the_embedder(mock_llm, spy_evidence, pinned_embedder):
    cfg = AppConfig.model_validate({"qa": {"llm": "openai/mock"}})  # default embedder
    a = stored("a")
    contexts = [
        *chunks_of(a.manifest.doc_id, {1: "Attention is a softmax."}),
        *chunks_of(a.manifest.doc_id, {2: "Heads run in parallel."}),
    ]
    answer = asyncio.run(
        PaperQAAnswerer(cfg).ask("What is attention?", [(a, "")], print, contexts=contexts)
    )
    assert pinned_embedder == []
    [(texts, _docs, retrieval)] = spy_evidence
    assert retrieval is False
    assert all(t.embedding is None for t in texts)
    assert answer.startswith("Question: What is attention?")


def test_contexts_answer_is_the_same_with_or_without_vectors(mock_llm, qa_cfg):
    a, b = stored("a"), stored("b")
    contexts = [
        *chunks_of(a.manifest.doc_id, {1: "Attention is a softmax over scaled dot products."}),
        *chunks_of(b.manifest.doc_id, {1: "A second paper."}),
    ]
    documents = [(a, ""), (b, "")]
    answer = asyncio.run(PaperQAAnswerer(qa_cfg).ask("What?", documents, print, contexts=contexts))

    class WithVectors(PaperQAAnswerer):
        """The previous behaviour: the given texts are embedded as they are added."""

        async def _docs_from_contexts(self, contexts, documents, settings):
            from paperqa import SparseEmbeddingModel

            docs = await super()._docs_from_contexts(contexts, documents, settings)
            vectors = await SparseEmbeddingModel().embed_documents([t.text for t in docs.texts])
            for text, vector in zip(docs.texts, vectors, strict=True):
                text.embedding = vector
            return docs

    with_vectors = asyncio.run(
        WithVectors(qa_cfg).ask("What?", documents, print, contexts=contexts)
    )
    assert answer == with_vectors


def test_without_contexts_the_embedder_is_built_once(mock_llm, pinned_embedder):
    cfg = AppConfig.model_validate({"qa": {"llm": "openai/mock"}})
    a = stored("a")
    markdown = "<!-- page 1 | method=text_layer -->\nAttention is a softmax.\n\n"
    asyncio.run(PaperQAAnswerer(cfg).ask("What is attention?", [(a, markdown)], print))
    assert pinned_embedder == ["st-pinned-minilm"]


def test_no_contexts_is_not_an_empty_corpus(qa_cfg):
    with pytest.raises(ValueError, match="contexts is empty"):
        asyncio.run(PaperQAAnswerer(qa_cfg).ask("q?", [(stored("a"), "")], print, contexts=[]))


def test_contexts_from_a_paper_that_was_not_given_are_refused(qa_cfg):
    contexts = chunks_of("c" * 64, {1: "Orphan."})
    with pytest.raises(ValueError, match="not in documents"):
        asyncio.run(
            PaperQAAnswerer(qa_cfg).ask("q?", [(stored("a"), "")], print, contexts=contexts)
        )


def test_without_contexts_paperqa_still_chunks_and_retrieves_itself(mock_llm, qa_cfg, spy_evidence):
    a = stored("a")
    pages = {1: "Attention is a softmax over scaled dot products.", 2: "Heads run in parallel."}
    markdown = "".join(f"<!-- page {n} | method=text_layer -->\n{t}\n\n" for n, t in pages.items())
    asyncio.run(PaperQAAnswerer(qa_cfg).ask("What is attention?", [(a, markdown)], print))
    [(texts, _docs, retrieval)] = spy_evidence
    assert retrieval is True
    assert [t.name for t in texts] == [c.name for c in chunks_of(a.manifest.doc_id, pages)]
