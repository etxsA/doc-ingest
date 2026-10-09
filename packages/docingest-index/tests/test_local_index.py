"""LocalIndex on real files: numpy, tantivy and a temporary folder, no mocks."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from docingest.domain.chunking import CHUNKER_VERSION, Chunk

from docingest_index import local
from docingest_index.local import FORMAT_VERSION, IndexBusyError, LocalIndex
from docingest_index.settings import EmbedderSettings, IndexSettings

DOC_A, DOC_B, DOC_C = "a" * 64, "b" * 64, "c" * 64
DOC_D, DOC_E = "d" * 64, "e" * 64
DIMS = 4


def unit(*values):
    v = np.asarray(values, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def chunk(doc_id, text, n=1, start=0, **kw):
    return Chunk(doc_id, f"{doc_id[:16]} pages {n}-{n}", text, n, n, start=start, **kw)


def make(root, embedder=None, **kw):
    embedder = embedder or EmbedderSettings(model="test/embed", revision="a" * 40)
    settings = IndexSettings(**kw.pop("settings", {}))
    corpus = kw.pop("corpus", Path(root).parent / (Path(root).name + "-corpus"))
    return LocalIndex(
        root,
        corpus=corpus,
        embedder=embedder,
        chunk_chars=900,
        overlap=100,
        settings=settings,
        **kw,
    )


def zebra_corpus(index):
    """Three chunks whose dense and keyword rankings disagree (hand-checked below)."""
    index.upsert(DOC_A, "k", [chunk(DOC_A, "alpha beta gamma")], [unit(1, 0, 0, 0)])
    index.upsert(DOC_B, "k", [chunk(DOC_B, "zebra stripes habitat")], [unit(0.8, 0.6, 0, 0)])
    index.upsert(DOC_C, "k", [chunk(DOC_C, "delta epsilon")], [unit(0, 0, 1, 0)])
    index.commit()


# ------------------------------------------------------------------ layout and round trip


def test_the_folder_has_the_documented_layout(tmp_path):
    index = make(tmp_path / "idx")
    chunks = [chunk(DOC_A, "first chunk"), chunk(DOC_A, "second", n=2, start=10, is_reference=True)]
    index.upsert(DOC_A, "k" * 64, chunks, [unit(1, 0, 0, 0), unit(0, 1, 0, 0)])
    index.commit()
    [folder] = (tmp_path / "idx").iterdir()
    assert folder == index.folder and folder.name.startswith("test-embed-")
    assert len(folder.name.rsplit("-", 1)[1]) == 8
    shards = sorted(p.name for p in (folder / "shards").iterdir())
    assert shards == [f"{'a' * 16}-{'k' * 16}.jsonl", f"{'a' * 16}-{'k' * 16}.npz"]
    assert sorted(p.name for p in (folder / "search").iterdir()) == [
        "bm25",
        "chunks.jsonl",
        "chunks.offsets.npy",
        "dense.npy",
        "ids.json",
    ]
    assert sorted(p.name for p in folder.iterdir()) == [
        ".lock",
        "fingerprint.json",
        "search",
        "shards",
        "state.json",
    ]
    lines = (folder / "shards" / shards[0]).read_text().splitlines()
    header, first, second = (json.loads(line) for line in lines)
    assert header == {
        "format": 1,
        "doc_id": DOC_A,
        "key": "k" * 64,
        "chunks": 2,
        "dims": DIMS,
        "seq": 1,  # the first shard written
    }
    assert first == {
        "name": chunks[0].name,
        "pages": [1, 1],
        "span": [0, 11],
        "is_reference": False,
        "text": "first chunk",
    }
    assert second["span"] == [10, 16] and second["is_reference"] is True
    dense = np.load(folder / "search" / "dense.npy")
    assert dense.dtype == np.float16 and dense.shape == (2, DIMS)
    with np.load(folder / "shards" / shards[1]) as data:
        assert data["vectors"].dtype == np.float16 and data["vectors"].shape == (2, DIMS)


def test_fingerprint_json_records_everything_the_folder_was_built_with(tmp_path):
    embedder = EmbedderSettings(model="test/embed", revision="a" * 40, query_instruction="web")
    index = make(tmp_path / "idx", embedder)
    index.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    record = json.loads((index.folder / "fingerprint.json").read_text())
    assert record == {
        "format": FORMAT_VERSION,
        "fingerprint": index.fingerprint,
        "corpus": str((tmp_path / "idx-corpus").resolve()),
        "model": "test/embed",
        "revision": "a" * 40,
        "dims": None,  # not seen before the first vectors: see the commit below
        "dtype": "float16",
        "query_instruction": "Given a web search query, retrieve relevant passages that answer the query",
        "chunker_version": CHUNKER_VERSION,
        "chunk_chars": 900,
        "overlap": 100,
    }


def test_the_vector_length_is_recorded_at_the_first_commit_and_kept(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    index.commit()
    path = index.folder / "fingerprint.json"
    assert json.loads(path.read_text())["dims"] == DIMS
    index.remove(DOC_A)
    index.commit()  # an index emptied after a commit still knows its vectors
    assert json.loads(path.read_text())["dims"] == DIMS
    with pytest.raises(ValueError, match="holds 4-dimensional"):
        make(tmp_path / "idx").upsert(DOC_B, "k", [chunk(DOC_B, "y")], [[1.0, 0.0]])


def test_state_json_has_the_counts_and_the_commit_time(tmp_path):
    index = make(tmp_path / "idx")
    assert not index.folder.exists()  # nothing is created before the first write
    zebra_corpus(index)
    state = json.loads((index.folder / "state.json").read_text())
    assert (state["documents"], state["chunks"], state["format"]) == (3, 3, 1)
    assert state["committed_at"].endswith("+00:00") and state["commit_seconds"] >= 0
    stats = index.stats()
    assert (stats.documents, stats.chunks, stats.searchable_documents) == (3, 3, 3)
    assert stats.committed_at == state["committed_at"]


def test_vectors_are_stored_as_float16_unit_vectors_and_search_reads_them_back(tmp_path):
    index = make(tmp_path / "idx", settings={"bm25": "never"})
    index.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [[3.0, 4.0, 0.0, 0.0]])  # not unit length
    index.commit()
    dense = np.load(index.folder / "search" / "dense.npy")
    assert dense.tolist() == [[0.60009765625, 0.7998046875, 0.0, 0.0]]  # float16 of (0.6, 0.8)
    [hit] = index.search("q", [6.0, 8.0, 0.0, 0.0], 5)  # the question is normalized too
    assert hit.score == pytest.approx(1.0, abs=1e-3)


def test_a_question_vector_of_unit_length_is_scored_as_it_is(tmp_path):
    index = make(tmp_path / "idx", settings={"bm25": "never"})
    v = np.random.default_rng(1).normal(size=DIMS).astype(np.float32)
    v /= np.linalg.norm(v) + np.float32(1e-12)
    index.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [v.tolist()])
    index.commit()
    [hit] = index.search("q", v.tolist(), 1)
    expected = np.float32(np.load(index.folder / "search" / "dense.npy")[0].astype(np.float32) @ v)
    assert hit.score == float(expected)  # not divided by a norm that is 1 only to rounding


def test_vectors_that_are_already_unit_length_are_not_touched(tmp_path):
    index = make(tmp_path / "idx")
    v = np.random.default_rng(0).normal(size=DIMS).astype(np.float32)
    v /= np.linalg.norm(v) + np.float32(1e-12)  # what the embedder returns
    index.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [v.tolist()])
    index.commit()
    assert np.array_equal(np.load(index.folder / "search" / "dense.npy")[0], v.astype(np.float16))


def test_a_new_instance_reads_everything_from_disk(tmp_path):
    zebra_corpus(make(tmp_path / "idx"))
    again = make(tmp_path / "idx")
    assert again.keys() == {DOC_A: "k", DOC_B: "k", DOC_C: "k"}
    [hit] = again.search("delta", unit(0, 0, 1, 0), 1)
    assert hit.chunk == chunk(DOC_C, "delta epsilon")


def test_ids_that_are_not_filename_safe_are_hashed_in_the_shard_name(tmp_path):
    index = make(tmp_path / "idx")
    odd = "../paper one/ü"
    index.upsert(odd, "key with spaces", [chunk(odd, "x")], [unit(1, 0, 0, 0)])
    index.commit()
    names = [p.name for p in (index.folder / "shards").iterdir()]
    assert all("/" not in n and " " not in n and ".." not in n for n in names)
    assert make(tmp_path / "idx").keys() == {odd: "key with spaces"}


# ------------------------------------------------------------------ incremental updates


def test_add_change_remove_and_the_commit_that_makes_them_visible(tmp_path):
    index = make(tmp_path / "idx")
    zebra_corpus(index)
    question = "zebra"
    q = unit(1, 0, 0, 0)
    assert {h.chunk.doc_id for h in index.search(question, q, 10)} == {DOC_A, DOC_B, DOC_C}

    index.upsert(DOC_A, "k2", [chunk(DOC_A, "completely new words")], [unit(0, 0, 0, 1)])
    index.remove(DOC_C)
    assert index.keys() == {DOC_A: "k2", DOC_B: "k"}
    assert {h.chunk.doc_id for h in index.search(question, q, 10)} == {DOC_A, DOC_B, DOC_C}
    assert "alpha beta gamma" in [h.chunk.text for h in index.search(question, q, 10)]
    stats = index.stats()
    assert (stats.documents, stats.searchable_documents) == (2, 3)

    index.commit()
    hits = index.search(question, q, 10)
    assert {h.chunk.doc_id for h in hits} == {DOC_A, DOC_B}
    assert "completely new words" in [h.chunk.text for h in hits]
    assert "alpha beta gamma" not in [h.chunk.text for h in hits]
    assert sorted(p.name for p in (index.folder / "shards").glob("*.jsonl")) == [
        f"{'a' * 16}-k2.jsonl",
        f"{'b' * 16}-k.jsonl",
    ]
    assert index.stats().searchable_documents == 2


def test_changing_a_document_deletes_its_old_shard_files(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(DOC_A, "old", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    index.upsert(DOC_A, "new", [chunk(DOC_A, "y")], [unit(1, 0, 0, 0)])
    assert sorted(p.name for p in (index.folder / "shards").iterdir()) == [
        f"{'a' * 16}-new.jsonl",
        f"{'a' * 16}-new.npz",
    ]
    index.upsert(
        DOC_A, "new", [chunk(DOC_A, "z")], [unit(1, 0, 0, 0)]
    )  # same key: replaced in place
    assert index.stats().chunks == 1


def test_removing_the_last_document_leaves_an_empty_searchable_index(tmp_path):
    index = make(tmp_path / "idx")
    zebra_corpus(index)
    for doc in (DOC_A, DOC_B, DOC_C):
        index.remove(doc)
    index.remove("never indexed")
    index.commit()
    assert index.keys() == {} and index.search("zebra", unit(1, 0, 0, 0), 5) == []
    assert index.stats().searchable_documents == 0 and index.stats().committed_at


def test_a_document_without_chunks_is_kept_and_searches_skip_it(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(DOC_A, "k", [], [])
    index.upsert(DOC_B, "k", [chunk(DOC_B, "zebra")], [unit(1, 0, 0, 0)])
    index.commit()
    assert index.keys() == {DOC_A: "k", DOC_B: "k"}
    assert [h.chunk.doc_id for h in index.search("zebra", unit(1, 0, 0, 0), 5)] == [DOC_B]
    assert (index.stats().documents, index.stats().searchable_documents) == (2, 2)


def test_an_interrupted_run_is_picked_up_by_the_next_process(tmp_path):
    first = make(tmp_path / "idx")
    first.upsert(DOC_A, "k", [chunk(DOC_A, "zebra")], [unit(1, 0, 0, 0)])  # never committed
    first.close()  # the process died
    second = make(tmp_path / "idx")
    assert second.keys() == {DOC_A: "k"}
    assert second.search("zebra", unit(1, 0, 0, 0), 5) == []
    second.commit()
    assert [h.chunk.doc_id for h in second.search("zebra", unit(1, 0, 0, 0), 5)] == [DOC_A]


def test_leftovers_of_an_interrupted_write_are_cleaned_up(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    index.close()
    shards = index.folder / "shards"
    (shards / "half.npz.tmp").write_bytes(b"partial")
    (shards / f"{'b' * 16}-orphan.npz").write_bytes(b"vectors without chunks")
    (shards / f"{'c' * 16}-broken.npz").write_bytes(b"x")
    (shards / f"{'c' * 16}-broken.jsonl").write_text("not json\n")
    again = make(tmp_path / "idx")
    assert again.keys() == {DOC_A: "k"}
    assert (shards / "half.npz.tmp").exists()  # reading deletes nothing ...
    assert (shards / f"{'b' * 16}-orphan.npz").exists()
    again.upsert(DOC_B, "k", [chunk(DOC_B, "y")], [unit(0, 1, 0, 0)])  # ... the first write does
    assert sorted(p.name for p in shards.glob("*.tmp")) == []
    assert not (shards / f"{'b' * 16}-orphan.npz").exists()
    assert (shards / f"{'c' * 16}-broken.jsonl").exists()  # unreadable: ignored, not deleted


def test_two_shards_for_one_document_keep_the_newer_one(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(DOC_A, "old", [chunk(DOC_A, "old text")], [unit(1, 0, 0, 0)])
    index.close()
    old = index.folder / "shards" / f"{'a' * 16}-old.jsonl"
    os.utime(old, ns=(1, 1))  # a crash left the old shard behind the new one
    stem_new = f"{'a' * 16}-new"
    for suffix in (".jsonl", ".npz"):
        src = index.folder / "shards" / f"{'a' * 16}-old{suffix}"
        (index.folder / "shards" / f"{stem_new}{suffix}").write_bytes(src.read_bytes())
    new = index.folder / "shards" / f"{stem_new}.jsonl"
    new.write_text(new.read_text().replace('"key": "old"', '"key": "new"'))
    again = make(tmp_path / "idx")
    assert again.keys() == {DOC_A: "new"}
    assert old.exists()  # a reader leaves the older shard alone ...
    again.commit()  # ... the next writer removes it
    assert not old.exists() and not old.with_suffix(".npz").exists()


def test_the_write_counter_decides_which_of_two_shards_is_newer_not_the_clock(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(DOC_A, "first", [chunk(DOC_A, "first text")], [unit(1, 0, 0, 0)])
    index.upsert(DOC_A, "second", [chunk(DOC_A, "second text")], [unit(1, 0, 0, 0)])
    index.close()
    shards = index.folder / "shards"
    # a crash between writing the second shard and deleting the first left both; the first
    # one now has the later modification time (a coarse clock, a restored backup, a copy)
    stem_first = f"{'a' * 16}-first"
    for suffix in (".jsonl", ".npz"):
        src = shards / f"{'a' * 16}-second{suffix}"
        (shards / f"{stem_first}{suffix}").write_bytes(src.read_bytes())
    first = shards / f"{stem_first}.jsonl"
    first.write_text(
        first.read_text()
        .replace('"key": "second"', '"key": "first"')
        .replace('"seq": 2', '"seq": 1')
    )
    os.utime(shards / f"{'a' * 16}-second.jsonl", ns=(1, 1))
    assert make(tmp_path / "idx").keys() == {DOC_A: "second"}


def test_every_write_gets_a_larger_counter_than_any_shard_on_disk(tmp_path):
    index = make(tmp_path / "idx")
    for n, doc in enumerate((DOC_A, DOC_B, DOC_A, DOC_C), start=1):
        index.upsert(doc, f"k{n}", [chunk(doc, f"text {n}")], [unit(1, 0, 0, 0)])
    index.commit()
    seqs = sorted(
        json.loads(p.read_text().splitlines()[0])["seq"]
        for p in (index.folder / "shards").glob("*.jsonl")
    )
    assert seqs == [2, 3, 4]  # DOC_A's first shard (1) went away with its replacement
    later = make(tmp_path / "idx")
    later.upsert(DOC_B, "again", [chunk(DOC_B, "again")], [unit(0, 1, 0, 0)])
    later.commit()
    top = max(
        json.loads(p.read_text().splitlines()[0])["seq"]
        for p in (later.folder / "shards").glob("*.jsonl")
    )
    assert top == 5


# ------------------------------------------------------------------ one generation per reader


def test_a_reader_that_stays_open_across_another_process_commit_keeps_one_generation(tmp_path):
    """The sequence of review 2 (r1): search in Spanish (no keyword part), another process
    adds papers and commits, search in English. BM25 used to open at the second search, so
    the row ids of the new commit were looked up in the old chunk records."""
    root = tmp_path / "idx"
    writer = make(root)
    zebra_corpus(writer)
    reader = make(root)
    spanish = reader.search("la cebra vive en la sabana africana", unit(0, 1, 0, 0), 10)
    assert {h.chunk.doc_id for h in spanish} == {DOC_A, DOC_B, DOC_C}

    other = make(root)  # the other process
    more = [chunk(DOC_D, f"zebra herds roam the savanna number {n}", n=n) for n in range(1, 7)]
    other.upsert(DOC_D, "k", more, [unit(0.9, 0.4, 0.1, 0) for _ in more])
    other.upsert(DOC_E, "k", [chunk(DOC_E, "zebra zebra zebra")], [unit(0.7, 0.7, 0, 0)])
    other.commit()

    english = reader.search("zebra stripes habitat", unit(0.8, 0.6, 0, 0), 10)
    assert {h.chunk.doc_id for h in english} == {DOC_A, DOC_B, DOC_C}  # still the old commit
    assert english[0].chunk.doc_id == DOC_B and english[0].chunk.text == "zebra stripes habitat"

    fresh = make(root).search("zebra stripes habitat", unit(0.8, 0.6, 0, 0), 10)
    assert {h.chunk.doc_id for h in fresh} >= {DOC_D, DOC_E}  # a new reader sees the new commit
    reader.close()  # closing drops the old generation: the next search opens the latest
    assert {h.chunk.doc_id for h in reader.search("zebra", unit(0.8, 0.6, 0, 0), 20)} >= {DOC_D}


def test_a_commit_that_lands_while_the_reader_opens_makes_it_start_over(tmp_path, monkeypatch):
    root = tmp_path / "idx"
    zebra_corpus(make(root))
    reader = make(root)
    original = local._Search.__init__
    raced = []

    def racing(self, folder, ids):
        original(self, folder, ids)
        if not raced:  # a commit between reading ids.json and having everything open
            raced.append(ids["generation"])
            other = make(root)
            other.upsert(DOC_D, "k", [chunk(DOC_D, "zebra savanna")], [unit(0.8, 0.6, 0, 0)])
            other.commit()

    monkeypatch.setattr(local._Search, "__init__", racing)
    hits = reader.search("zebra savanna", unit(0.8, 0.6, 0, 0), 10)
    assert len(raced) == 1
    assert DOC_D in {h.chunk.doc_id for h in hits}  # all of it from the generation after the race
    assert reader._search is not None and reader._search.rows == 4


def test_a_reader_gives_up_when_the_index_never_stops_changing(tmp_path, monkeypatch):
    root = tmp_path / "idx"
    zebra_corpus(make(root))
    reader = make(root)
    original = local._Search.__init__

    def racing(self, folder, ids):
        original(self, folder, ids)
        other = make(root)
        other.upsert(DOC_D, uuid_key(), [chunk(DOC_D, "zebra")], [unit(0.8, 0.6, 0, 0)])
        other.commit()

    monkeypatch.setattr(local._Search, "__init__", racing)
    with pytest.raises(IndexBusyError, match="kept changing"):
        reader.search("zebra", unit(0.8, 0.6, 0, 0), 10)


def uuid_key():
    import uuid

    return uuid.uuid4().hex


def test_the_keyword_index_is_open_before_the_first_keyword_question(tmp_path):
    index = make(tmp_path / "idx")
    zebra_corpus(index)
    index.search("la cebra vive en la sabana africana", unit(0, 1, 0, 0), 5)  # no keyword part
    assert index._search is not None and index._search._bm25 is not None


# ------------------------------------------------------------------ fingerprint and folders


def test_a_corpus_model_revision_or_chunker_change_is_another_folder(tmp_path):
    base = EmbedderSettings(model="test/embed", revision="a" * 40)
    folders = {
        make(tmp_path, base).folder,
        make(tmp_path, base.model_copy(update={"revision": "b" * 40})).folder,
        make(tmp_path, base, corpus=tmp_path / "another-corpus").folder,
        make(tmp_path, base.model_copy(update={"model": "test/other"})).folder,
        LocalIndex(
            tmp_path, corpus=tmp_path / "c", embedder=base, chunk_chars=1200, overlap=100
        ).folder,
        LocalIndex(
            tmp_path, corpus=tmp_path / "c", embedder=base, chunk_chars=900, overlap=50
        ).folder,
    }
    assert len(folders) == 6
    assert make(tmp_path, base).folder == make(tmp_path, base).folder


def test_the_query_instruction_and_transport_settings_do_not_change_the_folder(tmp_path):
    base = EmbedderSettings(model="test/embed", revision="a" * 40)
    other = base.model_copy(
        update={"query_instruction": "web", "base_url": "http://127.0.0.1:1/v1", "batch_size": 8}
    )
    plain, instructed = make(tmp_path, base), make(tmp_path, other)
    assert plain.folder == instructed.folder and plain.fingerprint == instructed.fingerprint
    plain.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    plain.commit()
    assert json.loads((plain.folder / "fingerprint.json").read_text())["query_instruction"] == ""
    instructed.commit()  # the instruction changed: recorded, no new folder, nothing re-embedded
    record = json.loads((plain.folder / "fingerprint.json").read_text())
    assert record["query_instruction"].startswith("Given a web search query")
    assert instructed.keys() == {DOC_A: "k"}


def test_indexes_of_two_embedders_never_mix_in_one_directory(tmp_path):
    a = make(tmp_path, EmbedderSettings(model="test/embed", revision="a" * 40))
    b = make(tmp_path, EmbedderSettings(model="test/embed", revision="b" * 40))
    a.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    assert b.keys() == {} and a.folder != b.folder
    assert a.embedder_fingerprint != b.embedder_fingerprint


def test_a_format_version_bump_would_be_a_new_folder(tmp_path, monkeypatch):
    before = make(tmp_path).folder
    monkeypatch.setattr(local, "FORMAT_VERSION", FORMAT_VERSION + 1)
    assert make(tmp_path).folder != before


# ------------------------------------------------------------------ validation


def test_bad_input_is_refused_before_anything_is_written(tmp_path):
    index = make(tmp_path / "idx")
    c = chunk(DOC_A, "x")
    with pytest.raises(ValueError, match="one vector per chunk"):
        index.upsert(DOC_A, "k", [c, c], [unit(1, 0, 0, 0)])
    with pytest.raises(ValueError, match="belong to"):
        index.upsert(DOC_B, "k", [c], [unit(1, 0, 0, 0)])
    with pytest.raises(ValueError, match="doc_id is empty"):
        index.upsert("", "k", [], [])
    with pytest.raises(ValueError, match="NaN"):
        index.upsert(DOC_A, "k", [c], [[float("nan"), 0.0, 0.0, 0.0]])
    assert not list((index.folder / "shards").glob("*"))
    index.close()
    zebra_corpus(index)
    with pytest.raises(ValueError, match="4-dimensional"):
        index.upsert(DOC_A, "k2", [c], [[1.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match="question vector has shape"):
        index.search("q", [1.0, 0.0], 3)
    assert index.search("q", unit(1, 0, 0, 0), 0) == []
    assert index.search("q", unit(1, 0, 0, 0), -1) == []


def test_search_before_any_commit_finds_nothing(tmp_path):
    assert make(tmp_path / "idx").search("q", unit(1, 0, 0, 0), 5) == []


# ------------------------------------------------------------------ the first stage


def ranking(hits):
    return [h.chunk.doc_id for h in hits]


def test_dense_and_keyword_rankings_fuse_by_rrf_on_an_english_question(tmp_path):
    index = make(tmp_path / "idx")
    zebra_corpus(index)
    hits = index.search("where is the zebra habitat", unit(1, 0, 0, 0), 3)
    # dense: A (cos 1.0), B (0.8), C (0).  keyword: only B has "zebra" or "habitat".
    # RRF: B = 1/62 + 1/61, A = 1/61, C = 1/63.
    assert ranking(hits) == [DOC_B, DOC_A, DOC_C]
    assert [h.score for h in hits] == pytest.approx([1 / 62 + 1 / 61, 1 / 61, 1 / 63])


def test_the_keyword_ranking_is_switched_by_the_bm25_setting_and_the_language(tmp_path):
    english, spanish = "where is the zebra habitat", "dónde vive la cebra zebra del hábitat"
    vector = unit(1, 0, 0, 0)
    never = make(tmp_path / "never", settings={"bm25": "never"})
    zebra_corpus(never)
    assert ranking(never.search(english, vector, 3)) == [DOC_A, DOC_B, DOC_C]  # dense only
    assert [h.score for h in never.search(english, vector, 3)] == pytest.approx(
        [1.0, 0.8, 0.0], abs=1e-3
    )

    auto = make(tmp_path / "auto")  # "english"
    zebra_corpus(auto)
    assert ranking(auto.search(spanish, vector, 3)) == [
        DOC_A,
        DOC_B,
        DOC_C,
    ]  # not English: dense only
    assert ranking(auto.search(english, vector, 3)) == [DOC_B, DOC_A, DOC_C]

    always = make(tmp_path / "always", settings={"bm25": "always"})
    zebra_corpus(always)
    assert ranking(always.search(spanish, vector, 3)) == [DOC_B, DOC_A, DOC_C]  # "zebra" matches


def test_the_keyword_ranking_stems_words_and_ignores_punctuation_and_case(tmp_path):
    index = make(tmp_path / "idx")
    zebra_corpus(index)
    # "ZEBRAS!" -> "zebras" -> stem "zebra"; the dense vector alone would prefer chunk A
    assert ranking(index.search("ZEBRAS!", unit(1, 0, 0, 0), 3))[0] == DOC_B
    # a question with no letters or digits has no keyword part: dense only, no error
    assert ranking(index.search("?!", unit(1, 0, 0, 0), 3)) == ranking(
        make(tmp_path / "idx", settings={"bm25": "never"}).search("?!", unit(1, 0, 0, 0), 3)
    )


def test_at_most_k_hits_even_when_both_rankings_are_longer(tmp_path):
    index = make(tmp_path / "idx")
    rng = np.random.default_rng(1)
    chunks = [chunk(DOC_A, f"filler words number {i} zebra", start=i * 10) for i in range(30)]
    index.upsert(DOC_A, "k", chunks, rng.normal(size=(30, DIMS)).tolist())
    index.commit()
    assert len(index.search("zebra", unit(1, 1, 1, 1), 7)) == 7
    assert len(index.search("zebra", unit(1, 1, 1, 1), 1000)) == 30


def test_the_per_paper_cap_limits_chunks_per_paper_and_defaults_to_none(tmp_path):
    def build(**settings):
        index = make(
            tmp_path / f"idx{len(settings)}{settings.get('max_chunks_per_paper', 0)}",
            settings=settings,
        )
        for doc, base in ((DOC_A, 1.0), (DOC_B, 0.5)):
            chunks = [chunk(doc, f"passage {i}", start=i * 20) for i in range(4)]
            vectors = [unit(base, 1.0 - i * 0.1, 0, 0) for i in range(4)]
            index.upsert(doc, "k", chunks, vectors)
        index.commit()
        return index

    question, vector = "passage", unit(1, 1, 0, 0)
    uncapped = build(bm25="never").search(question, vector, 8)
    assert len(uncapped) == 8
    capped = build(bm25="never", max_chunks_per_paper=2).search(question, vector, 8)
    assert [ranking(capped).count(d) for d in (DOC_A, DOC_B)] == [2, 2]
    # the cap keeps each paper's best chunks, in the order of the full ranking
    seen: dict[str, int] = {}
    expected = []
    for hit in uncapped:
        seen[hit.chunk.doc_id] = seen.get(hit.chunk.doc_id, 0) + 1
        if seen[hit.chunk.doc_id] <= 2:
            expected.append(hit.chunk)
    assert [h.chunk for h in capped] == expected
    assert len(build(bm25="never", max_chunks_per_paper=1).search(question, vector, 8)) == 2
    assert IndexSettings().max_chunks_per_paper == 0


def test_a_large_index_matches_a_brute_force_search_across_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "BLOCK_ROWS", 7)  # several blocks, the last one short
    rng = np.random.default_rng(7)
    index = make(tmp_path / "idx", settings={"bm25": "never"})
    total = 260
    vectors = rng.normal(size=(total, DIMS)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    for n in range(0, total, 20):
        doc = hashlib.sha256(str(n).encode()).hexdigest()
        chunks = [chunk(doc, f"text {n + i}", start=i * 100) for i in range(20)]
        index.upsert(doc, "k", chunks, vectors[n : n + 20].tolist())
    index.commit()
    query = rng.normal(size=DIMS).astype(np.float32)
    query /= np.linalg.norm(query)
    stored = vectors.astype(np.float16).astype(np.float32)  # what the index scores against
    expected = np.argsort(-(stored @ query), kind="stable")
    for k in (5, 100, 150):  # below, at and above the first-stage depth
        hits = index.search("q", query.tolist(), k)
        assert [h.chunk.text for h in hits] == [f"text {i}" for i in expected[:k]]
    assert len(index.search("q", query.tolist(), 1000)) == total


def test_two_documents_whose_ids_start_alike_do_not_overwrite_each_other(tmp_path):
    index = make(tmp_path / "idx")
    one, two = "f" * 16 + "1" * 48, "f" * 16 + "2" * 48
    index.upsert(one, "k", [chunk(one, "first paper")], [unit(1, 0, 0, 0)])
    index.upsert(two, "k", [chunk(two, "second paper")], [unit(0, 1, 0, 0)])
    index.commit()
    assert make(tmp_path / "idx").keys() == {one: "k", two: "k"}
    assert len(list((index.folder / "shards").glob("*.jsonl"))) == 2
    texts = {h.chunk.text for h in index.search("paper", unit(1, 1, 0, 0), 5)}
    assert texts == {"first paper", "second paper"}
    index.remove(one)
    assert make(tmp_path / "idx").keys() == {two: "k"}


# ------------------------------------------------------------------ isolation of search


def committed_pair(tmp_path):
    """A and B committed by one process; returns a function that opens a fresh reader."""
    writer = make(tmp_path / "idx")
    writer.upsert(DOC_A, "k1", [chunk(DOC_A, "old zebra text")], [unit(1, 0, 0, 0)])
    writer.upsert(DOC_B, "k1", [chunk(DOC_B, "other paper")], [unit(0, 1, 0, 0)])
    writer.commit()
    return lambda: make(tmp_path / "idx", settings={"bm25": "never"})


def texts(index):
    return sorted(h.chunk.text for h in index.search("q", unit(1, 1, 0, 0), 10))


def test_an_uncommitted_upsert_does_not_touch_what_a_fresh_reader_searches(tmp_path):
    reader = committed_pair(tmp_path)
    other = make(tmp_path / "idx")  # another process, stores A again under a new key
    other.upsert(DOC_A, "k2", [chunk(DOC_A, "new graph text")], [unit(0, 0, 1, 0)])
    assert texts(reader()) == ["old zebra text", "other paper"]  # no warm cache anywhere
    assert reader().keys() == {DOC_A: "k2", DOC_B: "k1"}


def test_an_uncommitted_remove_does_not_touch_what_a_fresh_reader_searches(tmp_path):
    reader = committed_pair(tmp_path)
    other = make(tmp_path / "idx")
    other.remove(DOC_A)
    assert texts(reader()) == ["old zebra text", "other paper"]
    assert reader().keys() == {DOC_B: "k1"}


def test_a_committed_remove_is_searched_for_the_first_time_after_the_commit(tmp_path):
    committed_pair(tmp_path)
    writer = make(tmp_path / "idx")
    writer.remove(DOC_A)
    writer.commit()
    assert texts(make(tmp_path / "idx")) == ["other paper"]  # the reader never searched before


def test_the_same_key_stored_again_keeps_old_text_with_old_vector_until_commit(tmp_path):
    reader = committed_pair(tmp_path)
    other = make(tmp_path / "idx")
    other.upsert(DOC_A, "k1", [chunk(DOC_A, "rewritten under the same key")], [unit(0, 0, 1, 0)])
    hits = reader().search("q", unit(1, 0, 0, 0), 1)  # the old vector is the best match
    assert [h.chunk.text for h in hits] == ["old zebra text"]
    other.commit()
    hits = make(tmp_path / "idx").search("q", unit(0, 0, 1, 0), 1)
    assert [h.chunk.text for h in hits] == ["rewritten under the same key"]


def test_search_reads_nothing_outside_the_search_folder(tmp_path):
    reader = committed_pair(tmp_path)
    shutil.rmtree(make(tmp_path / "idx").folder / "shards")
    assert texts(reader()) == ["old zebra text", "other paper"]


def test_a_reader_falls_back_to_the_old_search_folder_during_a_swap(tmp_path):
    committed_pair(tmp_path)
    folder = make(tmp_path / "idx").folder
    os.replace(folder / "search", folder / "search.old")  # the instant between the two renames
    assert texts(make(tmp_path / "idx")) == ["old zebra text", "other paper"]


# ------------------------------------------------------------------ pending changes


def test_pending_follows_the_shards_against_the_committed_search(tmp_path):
    index = make(tmp_path / "idx")
    assert index.stats().pending is False
    index.upsert(DOC_A, "k1", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    assert index.stats().pending is True  # stored, never committed
    index.commit()
    assert index.stats().pending is False
    index.upsert(DOC_A, "k1", [chunk(DOC_A, "y")], [unit(1, 0, 0, 0)])  # same key, new shard
    assert index.stats().pending is True
    index.close()
    other = make(tmp_path / "idx")
    assert other.stats().pending is True  # another process sees it too
    other.commit()
    assert make(tmp_path / "idx").stats().pending is False


# ------------------------------------------------------------------ readers and writers


def test_status_during_a_write_deletes_nothing(tmp_path):
    writer = make(tmp_path / "idx")
    writer.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    shards = writer.folder / "shards"
    half = shards / f"{'b' * 16}-half.npz.tmp"  # what a writer has in flight
    fresh = shards / f"{'b' * 16}-fresh.npz"  # its vectors, the .jsonl not yet there
    half.write_bytes(b"partial")
    fresh.write_bytes(b"vectors")
    before = sorted(p.name for p in shards.iterdir())
    reader = make(tmp_path / "idx")
    assert reader.keys() == {DOC_A: "k"} and reader.stats().pending is True
    reader.search("q", unit(1, 0, 0, 0), 5)
    assert sorted(p.name for p in shards.iterdir()) == before


def test_a_second_writer_is_refused_until_the_first_commits_or_closes(tmp_path):
    first, second = make(tmp_path / "idx"), make(tmp_path / "idx")
    first.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    with pytest.raises(IndexBusyError, match="another process is writing"):
        second.upsert(DOC_B, "k", [chunk(DOC_B, "y")], [unit(0, 1, 0, 0)])
    with pytest.raises(IndexBusyError):
        second.commit()
    with pytest.raises(IndexBusyError):
        second.remove(DOC_A)
    assert second.keys() == {DOC_A: "k"}  # reading is never refused
    first.commit()  # releases the lock
    second.upsert(DOC_B, "k", [chunk(DOC_B, "y")], [unit(0, 1, 0, 0)])
    second.close()
    first.upsert(DOC_C, "k", [chunk(DOC_C, "z")], [unit(0, 0, 1, 0)])
    assert set(first.keys()) == {DOC_A, DOC_B, DOC_C}  # first saw what second wrote


def test_close_gives_up_the_lock_after_writes_that_were_not_committed(tmp_path):
    first, second = make(tmp_path / "idx"), make(tmp_path / "idx")
    first.upsert(DOC_A, "k", [chunk(DOC_A, "x")], [unit(1, 0, 0, 0)])
    first.close()
    first.close()  # twice is fine
    second.upsert(DOC_B, "k", [chunk(DOC_B, "y")], [unit(0, 1, 0, 0)])
    second.commit()
    assert second.keys() == {DOC_A: "k", DOC_B: "k"}  # what first wrote is not lost by closing
    first.upsert(DOC_C, "k", [chunk(DOC_C, "z")], [unit(0, 0, 1, 0)])  # and first writes again
    assert set(first.keys()) == {DOC_A, DOC_B, DOC_C}


def test_close_after_a_search_releases_the_files_and_the_index_searches_again(tmp_path):
    index = make(tmp_path / "idx")
    zebra_corpus(index)
    assert index.search("zebra", unit(0, 1, 0, 0), 2)
    state = index._search
    records = state._records  # the file a search keeps open
    index.close()
    assert records is None or records.closed
    assert state.dense is None and state._bm25 is None  # nothing of the commit stays open
    assert index._search is None
    assert index.search("zebra", unit(0, 1, 0, 0), 2)


def test_removing_from_an_index_that_does_not_exist_creates_nothing(tmp_path):
    index = make(tmp_path / "idx")
    index.remove(DOC_A)
    assert not (tmp_path / "idx").exists()


# ------------------------------------------------------------------ crash safety of a rewrite


def test_a_crash_while_storing_a_paper_again_leaves_the_old_shard_usable(tmp_path, monkeypatch):
    writer = make(tmp_path / "idx")
    writer.upsert(
        DOC_A,
        "k",
        [chunk(DOC_A, "old one"), chunk(DOC_A, "old two", start=9)],
        [unit(1, 0, 0, 0)] * 2,
    )
    writer.commit()
    writer = make(tmp_path / "idx")
    real_replace = os.replace

    def crash_on_jsonl(src, dst):
        if str(dst).endswith(".jsonl"):
            raise OSError("killed")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_jsonl)
    with pytest.raises(OSError, match="killed"):
        writer.upsert(DOC_A, "k", [chunk(DOC_A, "new, a single chunk")], [unit(0, 1, 0, 0)])
    monkeypatch.undo()
    writer.close()
    again = make(tmp_path / "idx")
    assert again.keys() == {DOC_A: "k"} and again.stats().pending is False
    again.commit()  # no shape mismatch: the old pair is intact, the new vectors are an orphan
    assert texts(make(tmp_path / "idx")) == ["old one", "old two"]
    assert not any("-1" in p.name for p in (again.folder / "shards").iterdir())


def test_a_shard_whose_header_disagrees_with_its_vectors_is_reported_at_commit(tmp_path):
    index = make(tmp_path / "idx")
    index.upsert(
        DOC_A, "k", [chunk(DOC_A, "a"), chunk(DOC_A, "b", start=3)], [unit(1, 0, 0, 0)] * 2
    )
    stem = f"{'a' * 16}-k"
    np.savez(index.folder / "shards" / f"{stem}.npz", vectors=np.zeros((1, DIMS), dtype=np.float16))
    with pytest.raises(ValueError, match="store the paper again"):
        index.commit()
    shard = index.folder / "shards" / f"{stem}.jsonl"
    shard.write_text(shard.read_text().rsplit("\n", 2)[0] + "\n")  # now a record is missing
    with pytest.raises(ValueError, match="1 chunk records, its header says 2"):
        index.commit()
    assert not (index.folder / "search").exists()  # the failed commit swapped nothing in


# ------------------------------------------------------------------ one index per corpus


def test_two_corpora_with_the_same_dir_get_separate_folders_that_never_prune_each_other(tmp_path):
    one = make(tmp_path / "idx", corpus=tmp_path / "stage1")
    two = make(tmp_path / "idx", corpus=tmp_path / "qasper")
    assert one.folder != two.folder and one.folder.parent == two.folder.parent
    one.upsert(DOC_A, "k", [chunk(DOC_A, "stage one")], [unit(1, 0, 0, 0)])
    one.commit()
    assert two.keys() == {} and two.stats().documents == 0
    two.upsert(DOC_B, "k", [chunk(DOC_B, "qasper")], [unit(0, 1, 0, 0)])
    two.commit()
    assert one.keys() == {DOC_A: "k"}
    assert json.loads((one.folder / "fingerprint.json").read_text())["corpus"] == str(
        (tmp_path / "stage1").resolve()
    )
    assert make(tmp_path / "idx", corpus=tmp_path / "stage1").folder == one.folder
