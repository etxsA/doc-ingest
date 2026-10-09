"""LocalIndex.search against a copy of the measured end-to-end retrieval.

``reference`` below is the first stage of ``retrieve.py`` of the end-to-end test (exact cosine
top 100 over the float16 vectors, tantivy ``en_stem`` BM25 top 100 on the lowercased
[A-Za-z0-9] words of the question, reciprocal rank fusion with k = 60, first 50), kept apart
from the code under test so a change that alters the ranking fails here. It differs from the
script in three harmless ways: the BM25 index is built with one writer thread and a 50 MB heap
(the script used the default threads and 500 MB; the same one-thread build is what makes ties
reproducible), ``rrf`` returns ``(row, score)`` pairs where the script returns ids, and every
text starts with a ``c<row>`` token that names the row.
"""

import re
import zlib

import numpy as np
import pytest
import tantivy
from docingest.domain.chunking import Chunk

from docingest_index.local import LocalIndex
from docingest_index.settings import EmbedderSettings, IndexSettings

DIMS = 32
VOCABULARY = [f"term{i}" for i in range(80)] + [
    "qubit",
    "qubits",
    "coherence",
    "transmon",
    "resonator",
    "fidelity",
    "gate",
    "gates",
    "readout",
    "noise",
]
QUESTIONS = [
    "what is the coherence of a transmon qubit",
    "how do gates and readout limit fidelity",
    "which term3 and term17 appear with noise",
    "the resonators",
    "Qubits, qubit gates: term41?",
]


def rrf(a, b, k=60, n=100):  # from retrieve.py
    s = {}
    for lst in (a, b):
        for r, d in enumerate(lst):
            s[d] = s.get(d, 0.0) + 1.0 / (k + r + 1)
    return [(d, v) for d, v in sorted(s.items(), key=lambda x: -x[1])[:n]]


def reference(texts, stored, query, question, tmp_path):
    D = stored.astype(np.float32)
    v = np.asarray(query, dtype=np.float32)
    v /= np.linalg.norm(v) + 1e-12
    s = D @ v
    idx = np.argpartition(-s, 100)[:100]
    dense = [int(j) for j in idx[np.argsort(-s[idx], kind="stable")]]
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("i", stored=True)
    sb.add_text_field("text", stored=False, tokenizer_name="en_stem")
    path = tmp_path / "reference-bm25"
    if not path.exists():
        path.mkdir()
        index = tantivy.Index(sb.build(), path=str(path))
        w = index.writer(heap_size=50_000_000, num_threads=1)  # as the index: one segment
        for i, t in enumerate(texts):
            w.add_document(tantivy.Document(i=i, text=t))
        w.commit()
        w.wait_merging_threads()
    index = tantivy.Index.open(str(path))
    index.reload()
    searcher = index.searcher()
    clean = " ".join(re.findall(r"[A-Za-z0-9]+", question)).lower() or "empty"
    bm = [
        int(searcher.doc(h)["i"][0])
        for _, h in searcher.search(index.parse_query(clean, ["text"]), 100).hits
    ]
    return rrf(dense, bm)[:50], dense[:50]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    rng = np.random.default_rng(20261008)
    tmp = tmp_path_factory.mktemp("oracle")
    embedder = EmbedderSettings(model="test/embed", revision="a" * 40)
    index = LocalIndex(
        tmp / "idx", corpus=tmp / "corpus", embedder=embedder, chunk_chars=900, overlap=100
    )
    texts, vectors = [], []
    for d in range(25):
        doc = f"{d:02d}" + "e" * 62  # sorted ids: the row order of the index is the insertion order
        chunks, vecs = [], []
        for c in range(16):
            row = len(texts) + len(chunks)
            words = rng.choice(VOCABULARY, size=14)
            text = f"c{row} " + " ".join(words)
            chunks.append(Chunk(doc, f"{doc[:16]} pages 1-1", text, 1, 1, start=c * 800))
            v = rng.normal(size=DIMS).astype(np.float32)
            vecs.append(v / (np.linalg.norm(v) + np.float32(1e-12)))
        index.upsert(doc, "k", chunks, [v.tolist() for v in vecs])
        texts += [c.text for c in chunks]
        vectors += vecs
    index.commit()
    stored = np.vstack(vectors).astype(np.float16)
    return index, texts, stored, tmp


@pytest.mark.parametrize("question", QUESTIONS)
def test_the_fused_top_50_is_the_one_of_the_measured_script(corpus, question):
    index, texts, stored, tmp = corpus
    query = np.random.default_rng(zlib.crc32(question.encode())).normal(size=DIMS)
    wanted, _ = reference(texts, stored, query, question, tmp)
    hits = index.search(question, query.tolist(), 50)
    assert [int(h.chunk.text.split()[0][1:]) for h in hits] == [row for row, _ in wanted]
    assert [h.score for h in hits] == pytest.approx([score for _, score in wanted], rel=1e-12)


def test_with_the_keyword_part_off_the_top_50_is_the_dense_ranking(corpus):
    _, texts, stored, tmp = corpus
    off = LocalIndex(
        tmp / "idx",
        corpus=tmp / "corpus",
        embedder=EmbedderSettings(model="test/embed", revision="a" * 40),
        chunk_chars=900,
        overlap=100,
        settings=IndexSettings(bm25="never"),
    )
    query = np.random.default_rng(3).normal(size=DIMS)
    _, dense = reference(texts, stored, query, "the qubit", tmp)
    hits = off.search("the qubit", query.tolist(), 50)
    assert [int(h.chunk.text.split()[0][1:]) for h in hits] == dense
