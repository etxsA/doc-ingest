"""Two writers of one index through IndexService: the second fails before it embeds anything."""

import pytest
from docingest.application.index import IndexService
from fakes import FakeEmbedder, InMemoryStore, add_doc

from docingest_index.local import IndexBusyError, LocalIndex

PAPER_A, PAPER_B = "a" * 64, "b" * 64


@pytest.fixture
def store():
    s = InMemoryStore()
    add_doc(s, PAPER_A, [("Attention weights use a softmax over scaled dot products. " * 4, "A")])
    add_doc(s, PAPER_B, [("Qubits decohere when they couple to the environment. " * 4, "B")])
    return s


def service(store, tmp_path, embedder_settings, embedder_class=FakeEmbedder, **kw):
    index = LocalIndex(
        tmp_path / "index",
        corpus=tmp_path / "corpus",
        embedder=embedder_settings,
        chunk_chars=60,
        overlap=10,
    )
    embedder = embedder_class(fingerprint=index.embedder_fingerprint, **kw)
    return (
        IndexService(
            store=store,
            embedder=embedder,
            index=index,
            chunk_chars=60,
            overlap=10,
            log=lambda _: None,
        ),
        embedder,
    )


class RivalEmbedder(FakeEmbedder):
    """While it embeds (the first build is under way) a second build is started."""

    def __init__(self, rival, method="build", **kw):
        super().__init__(**kw)
        self.rival, self.method, self.outcome = rival, method, None

    def embed_documents(self, texts):
        if self.outcome is None:
            try:
                args = (self.rival.store.corpus()[0],) if self.method == "update" else ()
                getattr(self.rival, self.method)(*args)
            except IndexBusyError as e:
                self.outcome = e
            else:
                self.outcome = "ran"
        return super().embed_documents(texts)


@pytest.mark.parametrize("method", ["build", "update"])
def test_a_second_build_fails_at_once_and_embeds_nothing(
    store, tmp_path, embedder_settings, method
):
    second, second_embedder = service(store, tmp_path, embedder_settings)
    first, first_embedder = service(
        store, tmp_path, embedder_settings, RivalEmbedder, rival=second, method=method
    )
    args = (store.corpus()[0],) if method == "update" else ()
    getattr(first, method)(*args)
    assert isinstance(first_embedder.outcome, IndexBusyError)
    assert "another process is writing" in str(first_embedder.outcome)
    assert second_embedder.calls == 0
    assert first.index.stats().searchable_documents == 2  # the first build finished
    # The lock went with the first build: the second can run now, and finds nothing to do.
    report = getattr(second, method)(*args)
    assert (report.added, report.unchanged) == (0, 2) and second_embedder.calls == 0


def test_a_build_that_fails_to_lock_leaves_nothing_behind(store, tmp_path, embedder_settings):
    holder, _ = service(store, tmp_path, embedder_settings)
    other, embedder = service(store, tmp_path, embedder_settings)
    holder.index.begin_write()
    with pytest.raises(IndexBusyError):
        other.build()
    assert embedder.calls == 0 and other.index.keys() == {}
    holder.index.close()
    assert other.build().added == 2
