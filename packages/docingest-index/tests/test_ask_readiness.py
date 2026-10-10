"""The check before a question on a real LocalIndex whose shards are damaged from outside."""

import asyncio

import pytest
from docingest.application.ask import AskService, Retrieval
from docingest.application.index import IndexService
from fakes import FakeEmbedder, FakeQA, InMemoryStore, add_doc

from docingest_index.local import LocalIndex

PAPER_A, PAPER_B = "a" * 64, "b" * 64


def fresh_index(index):
    """Another object on the same folder: the first one keeps the shards it read."""
    return LocalIndex(
        index.folder.parent,
        corpus=index.corpus,
        embedder=index._embedder,
        chunk_chars=60,
        overlap=10,
    )


@pytest.fixture
def built(tmp_path, embedder_settings):
    store = InMemoryStore()
    add_doc(
        store, PAPER_A, [("Attention weights use a softmax over scaled dot products. " * 4, "A")]
    )
    add_doc(store, PAPER_B, [("Qubits decohere when they couple to the environment. " * 4, "B")])
    index = LocalIndex(
        tmp_path / "index",
        corpus=tmp_path / "corpus",
        embedder=embedder_settings,
        chunk_chars=60,
        overlap=10,
    )
    embedder = FakeEmbedder(fingerprint=index.embedder_fingerprint)
    IndexService(
        store=store,
        embedder=embedder,
        index=index,
        chunk_chars=60,
        overlap=10,
        log=lambda _: None,
    ).build()

    def ask():
        retrieval = Retrieval(embedder, fresh_index(index), None, 50, 10)
        warnings: list[str] = []
        service = AskService(store=store, qa=FakeQA(), retrieval=lambda: retrieval)
        asyncio.run(service.ask("qubits decohere", warnings.append))
        return warnings

    return index, ask


def test_an_index_that_is_up_to_date_warns_of_nothing(built):
    _, ask = built
    assert ask() == []


def test_a_stored_but_uncommitted_document_still_warns_of_pending_changes(built):
    index, ask = built
    index.remove(PAPER_A)
    index.close()
    warnings = ask()
    assert any("not committed" in w for w in warnings)
    assert any("1 of 2 documents" in w for w in warnings)  # the removed one, as before


def test_a_corrupt_shard_the_commit_does_not_know_is_not_a_pending_change(built):
    index, ask = built
    shards = index.folder / "shards"
    (shards / "zzzz-0.jsonl").write_text("not a header\n")
    (shards / "zzzz-0.npz").write_bytes(b"")
    fresh = fresh_index(index)
    assert fresh.committed().pending is True  # the file names alone cannot tell
    assert fresh.stats().pending is False  # the header cannot be read: not a shard
    assert ask() == []  # ask asks stats() as well, so it does not warn after every build


def test_a_corrupt_shard_the_commit_holds_is_still_answered_from_the_commit(built):
    index, ask = built
    first = sorted((index.folder / "shards").glob("*.jsonl"))[0]
    first.write_text("not a header\n")
    fresh = fresh_index(index)
    assert fresh.stats().pending is True and fresh.committed().pending is False
    assert ask() == []  # the documented difference: only stats() and keys() notice it
