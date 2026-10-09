"""ChunkIndex adapter: vectors and chunks on disk, exact dense search plus tantivy BM25.

Layout, per index. One folder per corpus, embedder and chunker setting, so nothing is mixed::

    <dir>/<model-slug>-<fingerprint8>/
      fingerprint.json     what the folder holds (corpus, model, revision, the vector length
                           seen at the first commit, dtype, query instruction, chunker version
                           and settings, format version)
      shards/<doc_id[:16]>-<key[:16]>.npz    float16 unit vectors of one paper
      shards/<doc_id[:16]>-<key[:16]>.jsonl  a header line, then one line per chunk
      search/dense.npy, chunks.jsonl, chunks.offsets.npy, ids.json, bm25/
                           everything ``search`` reads, rebuilt from the shards by commit()
      state.json           documents, chunks and the time of the last commit
      .lock                held by the process that writes

Isolation. ``search`` reads only ``search/``, which holds its own copy of the vectors and of
the chunk records, so what it answers is the last commit whatever ``upsert`` and ``remove``
do to the shards afterwards, in this process or in another. Reading never changes a file.
A reader opens one commit whole (vectors, chunk records and BM25 index) when it first
searches and keeps that commit until ``close`` or a new object, whatever other processes
commit meanwhile; ``ids.json`` names the commit's generation, so an open that a commit
interrupts is noticed and starts over.

Writing. A write (``upsert``, ``remove``, ``commit``) takes an exclusive lock on ``.lock``
and holds it until ``commit`` returns or ``close`` is called; a second writer fails at once
with ``IndexBusyError`` (readers are not locked). Under the lock the first write cleans up
what an interrupted writer left: temporary files, vectors without chunks, the older of two
shards of one paper (the one with the smaller write counter ``seq`` in its header). A shard
is valid once its ``.jsonl`` exists: ``upsert`` writes the ``.npz`` first and the ``.jsonl``
last through temporary files, always under a stem no other shard uses (also when it stores
the same key again), and deletes the paper's old shard only after that, so a crash leaves
the old shard or the new one, never a mixture.

``commit`` rebuilds ``search/`` next to the old one and swaps it in. ``stats().pending`` says
whether the shards differ from what ``search/`` was built from (in documents or in the shard
of any document), which counts alone cannot tell.

``search`` is the measured first stage: exact cosine top 100, BM25 top 100 for questions that
look English (``[index] bm25``), both fused by reciprocal rank fusion (k = 60).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import numpy as np
from docingest.domain.chunking import CHUNKER_VERSION
from docingest.domain.errors import DocingestError
from docingest.ports import Chunk, Hit, IndexStats, KeywordMode, Vector

from .embedder import embedder_fingerprint, query_task
from .fusion import rrf
from .language import looks_english
from .settings import EmbedderSettings, IndexSettings

FORMAT_VERSION = 1
DTYPE = "float16"
LIST_DEPTH = 100  # length of each first-stage ranking before fusion
BLOCK_ROWS = 16384  # rows of the dense matrix scored at once (float32 copy of one block)
OPEN_ATTEMPTS = 5  # tries to open a search folder that other processes keep replacing
BM25_HEAP_BYTES = 200_000_000
UNIT_TOLERANCE = 1e-4  # a vector this close to length 1 is stored as it is
_SAFE = re.compile(r"[0-9A-Za-z_-]+")
_WORDS = re.compile(r"[A-Za-z0-9]+")


class IndexBusyError(DocingestError):
    """Another process is writing to this index folder."""


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") or "model"


def _part(value: str) -> str:
    """The first 16 characters of an id when they are filename-safe, else a hash of it."""
    if _SAFE.fullmatch(value):
        return value[:16]
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@dataclass(frozen=True)
class _Shard:
    doc_id: str
    key: str
    stem: str
    chunks: int
    dims: int
    seq: int = 0  # the order shards were written in: the larger one is the newer


class LocalIndex:
    def __init__(
        self,
        root: Path | str,
        *,
        corpus: Path | str,
        embedder: EmbedderSettings,
        chunk_chars: int,
        overlap: int,
        settings: IndexSettings | None = None,
    ):
        """``corpus`` names the document store the index mirrors (the factory passes the
        resolved ``output_dir``). It is part of the identity: two corpora that share ``root``
        get separate folders, so building one never prunes or mixes the other. A corpus that
        moves is a new index."""
        self.settings = settings or IndexSettings()
        self.embedder_fingerprint = embedder_fingerprint(embedder)
        self.corpus = str(Path(corpus).resolve())
        self._identity: dict[str, Any] = {
            "format": FORMAT_VERSION,
            "corpus": self.corpus,
            "embedder": self.embedder_fingerprint,
            "dtype": DTYPE,
            "chunker": {"version": CHUNKER_VERSION, "chunk_chars": chunk_chars, "overlap": overlap},
        }
        self.fingerprint = "local-index " + json.dumps(self._identity, sort_keys=True)
        digest = hashlib.sha256(self.fingerprint.encode()).hexdigest()[:8]
        self.folder = Path(root) / f"{slug(embedder.model)}-{digest}"
        self._embedder = embedder
        self._chunk_chars, self._overlap = chunk_chars, overlap
        self._loaded: dict[str, _Shard] | None = None
        self._search: _Search | None = None
        self._lock: IO[bytes] | None = None

    # ------------------------------------------------------------------ ChunkIndex

    def keys(self) -> dict[str, str]:
        return {doc_id: shard.key for doc_id, shard in self._shards().items()}

    def upsert(
        self, doc_id: str, key: str, chunks: Sequence[Chunk], vectors: Sequence[Vector]
    ) -> None:
        if not doc_id:
            raise ValueError("doc_id is empty")
        if len(chunks) != len(vectors):
            raise ValueError(f"need one vector per chunk, got {len(vectors)} for {len(chunks)}")
        if any(c.doc_id != doc_id for c in chunks):
            raise ValueError(f"every chunk must belong to {doc_id!r}")
        shards = self._writer()
        matrix = self._unit_float16(vectors)
        self._write_identity()
        stem = self._free_stem(doc_id, key)
        seq = max((s.seq for s in shards.values()), default=0) + 1
        shard_dir = self.folder / "shards"
        shard_dir.mkdir(exist_ok=True)
        npz = shard_dir / f"{stem}.npz"
        tmp = npz.with_name(npz.name + ".tmp")
        with tmp.open("wb") as f:
            np.savez(f, vectors=matrix)
        os.replace(tmp, npz)
        header = {
            "format": FORMAT_VERSION,
            "doc_id": doc_id,
            "key": key,
            "chunks": len(chunks),
            "dims": int(matrix.shape[1]),
            "seq": seq,
        }
        lines = [json.dumps(header, ensure_ascii=False)]
        lines += [
            json.dumps(
                {
                    "name": c.name,
                    "pages": [c.first_page, c.last_page],
                    "span": [c.start, c.start + len(c.text)],
                    "is_reference": c.is_reference,
                    "text": c.text,
                },
                ensure_ascii=False,
            )
            for c in chunks
        ]
        _atomic_write(shard_dir / f"{stem}.jsonl", ("\n".join(lines) + "\n").encode("utf-8"))
        previous = shards.get(doc_id)
        if previous is not None:
            self._delete_files(previous.stem)  # only now: the new shard is complete
        shards[doc_id] = _Shard(doc_id, key, stem, len(chunks), int(matrix.shape[1]), seq)

    def remove(self, doc_id: str) -> None:
        if not self.folder.is_dir():
            return
        shard = self._writer().pop(doc_id, None)
        if shard is not None:
            self._delete_files(shard.stem)

    def commit(self) -> None:
        started = time.perf_counter()
        self._writer()
        try:
            self._commit(started)
        finally:
            self.close()

    def close(self) -> None:
        """Release the write lock and the files a search keeps open. ``commit`` does it; call
        this to give up after writing without committing, and after reading. The index stays
        usable: the next call takes again what it needs."""
        if self._lock is not None:
            self._lock.close()  # closing the file releases the flock
            self._lock = None
        if self._search is not None:
            self._search.close()
            self._search = None

    def stats(self) -> IndexStats:
        shards = self._shards()
        found = self._read_ids()
        committed_at = None
        with contextlib.suppress(OSError, ValueError, KeyError):
            committed_at = json.loads((self.folder / "state.json").read_text())["committed_at"]
        if found is None:
            searchable, pending = (0, 0), bool(shards)
        else:
            ids = found[1]
            searchable = (ids["documents"], ids["rows"])
            built = {(s["doc_id"], s["stem"]) for s in ids["shards"]}
            pending = built != {(s.doc_id, s.stem) for s in shards.values()}
        return IndexStats(
            documents=len(shards),
            chunks=sum(s.chunks for s in shards.values()),
            searchable_documents=searchable[0],
            searchable_chunks=searchable[1],
            committed_at=committed_at,
            pending=pending,
        )

    def search(
        self, question: str, vector: Vector, k: int, *, keywords: KeywordMode | None = None
    ) -> list[Hit]:
        if k <= 0:
            return []
        state = self._open_search()
        if state is None or state.rows == 0:
            return []
        query = state.query(vector)
        depth = max(LIST_DEPTH, k)
        dense, cosine = state.dense_top(query, depth)
        if self._wants_bm25(question, keywords or self.settings.bm25):
            ranked = rrf(dense, state.bm25_top(question, depth), n=depth)
        else:
            ranked = [(row, float(cosine[row])) for row in dense]
        cap = self.settings.max_chunks_per_paper
        taken: list[tuple[int, float]] = []
        per_paper: Counter[str] = Counter()
        for row, score in ranked:
            if cap:
                doc_id = state.doc_of(row)
                if per_paper[doc_id] >= cap:
                    continue
                per_paper[doc_id] += 1
            taken.append((row, score))
            if len(taken) == k:
                break
        return [Hit(state.chunk(row), score) for row, score in taken]

    # ------------------------------------------------------------------ writing

    def _writer(self) -> dict[str, _Shard]:
        """Take the write lock if this process does not hold it, clean up after an
        interrupted writer, and return the shards as they are on disk now."""
        if self._lock is None:
            self.folder.mkdir(parents=True, exist_ok=True)
            handle = (self.folder / ".lock").open("ab")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise IndexBusyError(
                    f"another process is writing to the index {self.folder}; wait for it to finish"
                ) from None
            self._lock = handle
            self._loaded = self._cleanup()
        return self._shards()

    def _cleanup(self) -> dict[str, _Shard]:
        """What an interrupted writer left: temporary files, vectors without chunks, the older
        shard of a paper with two, a half-swapped ``search/``. Needs the lock."""
        shard_dir = self.folder / "shards"
        winners, losers, orphans = self._scan_all()
        if shard_dir.is_dir():
            for leftover in shard_dir.glob("*.tmp"):
                leftover.unlink(missing_ok=True)
        for stem in losers:
            self._delete_files(stem)
        for stem in orphans:
            (shard_dir / f"{stem}.npz").unlink(missing_ok=True)
        shutil.rmtree(self.folder / "search.new", ignore_errors=True)
        if (self.folder / "search" / "ids.json").exists():
            shutil.rmtree(self.folder / "search.old", ignore_errors=True)
        return winners

    def _free_stem(self, doc_id: str, key: str) -> str:
        """``<doc_id[:16]>-<key[:16]>``, or that with ``-1``, ``-2``... when a shard already
        has the name (ids that share their first 16 characters, or the same key stored again:
        the new shard is complete before the old one goes)."""
        base = f"{_part(doc_id)}-{_part(key)}"
        shard_dir = self.folder / "shards"
        n = 0
        while True:
            stem = base if n == 0 else f"{base}-{n}"
            if not any((shard_dir / f"{stem}{suffix}").exists() for suffix in (".jsonl", ".npz")):
                return stem
            n += 1

    def _delete_files(self, stem: str) -> None:
        for suffix in (".jsonl", ".npz"):  # the marker first: a half-deleted shard is invalid
            (self.folder / "shards" / f"{stem}{suffix}").unlink(missing_ok=True)

    def _write_identity(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        e = self._embedder
        record = {
            "format": FORMAT_VERSION,
            "fingerprint": self.fingerprint,
            "corpus": self.corpus,
            "model": e.model,
            "revision": e.revision,
            "dims": self._known_dims() or None,  # seen at the first commit, then validated
            "dtype": DTYPE,
            "query_instruction": query_task(e.query_instruction),  # recorded, not in the key
            "chunker_version": CHUNKER_VERSION,
            "chunk_chars": self._chunk_chars,
            "overlap": self._overlap,
        }
        path = self.folder / "fingerprint.json"
        text = json.dumps(record, indent=2, sort_keys=True) + "\n"
        if not path.exists() or path.read_text() != text:
            _atomic_write(path, text.encode())

    def _known_dims(self) -> int:
        """The vector length: from a stored shard, else the one recorded in
        ``fingerprint.json`` (an index emptied after a commit keeps it), else 0 (not seen)."""
        from_shards = max((s.dims for s in self._shards().values()), default=0)
        if from_shards:
            return from_shards
        with contextlib.suppress(OSError, ValueError, TypeError):
            return int(json.loads((self.folder / "fingerprint.json").read_text())["dims"] or 0)
        return 0

    def _unit_float16(self, vectors: Sequence[Vector]) -> np.ndarray:
        dims = self._known_dims()
        if len(vectors) == 0:
            return np.zeros((0, dims), dtype=np.float16)
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or (dims and matrix.shape[1] != dims):
            raise ValueError(
                f"vectors have shape {matrix.shape}, the index holds {dims}-dimensional vectors"
            )
        if not np.isfinite(matrix).all():
            raise ValueError("vectors contain NaN or infinity")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        odd = np.abs(norms - 1.0).ravel() > UNIT_TOLERANCE  # already unit vectors stay as they are
        matrix[odd] /= norms[odd] + np.float32(1e-12)
        return matrix.astype(np.float16)

    def _commit(self, started: float) -> None:
        shards = sorted(self._shards().values(), key=lambda s: (s.doc_id, s.stem))
        self._write_identity()
        new = self.folder / "search.new"
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        rows = sum(s.chunks for s in shards)
        dims = self._known_dims()
        (new / "ids.json").write_text(
            json.dumps(
                {
                    "format": FORMAT_VERSION,
                    "generation": uuid.uuid4().hex,  # one per commit: a reader checks it
                    "documents": len(shards),
                    "rows": rows,
                    "dims": dims,
                    "shards": [
                        {"doc_id": s.doc_id, "stem": s.stem, "chunks": s.chunks} for s in shards
                    ],
                }
            )
        )
        if rows:
            self._write_search_files(new, shards, rows, dims)
        current, old = self.folder / "search", self.folder / "search.old"
        shutil.rmtree(old, ignore_errors=True)
        if current.exists():
            os.replace(current, old)
        os.replace(new, current)
        shutil.rmtree(old, ignore_errors=True)
        self._search = None
        state = {
            "format": FORMAT_VERSION,
            "documents": len(shards),
            "chunks": rows,
            "committed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit_seconds": round(time.perf_counter() - started, 3),
        }
        _atomic_write(self.folder / "state.json", json.dumps(state).encode())

    def _write_search_files(self, new: Path, shards: list[_Shard], rows: int, dims: int) -> None:
        """The vectors, the chunk records and the BM25 index of every shard, in row order."""
        import tantivy

        builder = tantivy.SchemaBuilder()
        builder.add_unsigned_field("i", stored=True)
        builder.add_text_field("text", stored=False, tokenizer_name="en_stem")
        (new / "bm25").mkdir()
        index = tantivy.Index(builder.build(), path=str(new / "bm25"))
        # one thread, one segment: BM25 ties break the same way in every build
        writer = index.writer(heap_size=BM25_HEAP_BYTES, num_threads=1)
        dense = np.lib.format.open_memmap(
            new / "dense.npy", mode="w+", dtype=np.float16, shape=(rows, dims)
        )
        offsets = [0]
        row = 0
        with (new / "chunks.jsonl").open("wb") as records:
            for shard in shards:
                lines = self._shard_lines(shard)
                with np.load(self.folder / "shards" / f"{shard.stem}.npz") as data:
                    part = data["vectors"]
                if shard.chunks and part.shape != (shard.chunks, dims):
                    raise ValueError(
                        f"shard {shard.stem} holds vectors of shape {part.shape} for "
                        f"{shard.chunks} chunks of {dims} dimensions; store the paper again"
                    )
                if shard.chunks:
                    dense[row : row + shard.chunks] = part
                for line in lines:  # the record as the shard has it, now inside search/
                    records.write(line)
                    offsets.append(offsets[-1] + len(line))
                    writer.add_document(tantivy.Document(i=row, text=json.loads(line)["text"]))
                    row += 1
        dense.flush()
        del dense
        writer.commit()
        writer.wait_merging_threads()
        np.save(new / "chunks.offsets.npy", np.asarray(offsets, dtype=np.int64))

    def _shard_lines(self, shard: _Shard) -> list[bytes]:
        """The chunk records of a shard, as written (without the header line)."""
        with (self.folder / "shards" / f"{shard.stem}.jsonl").open("rb") as f:
            f.readline()
            lines = f.readlines()
        if len(lines) != shard.chunks:
            raise ValueError(
                f"shard {shard.stem} has {len(lines)} chunk records, its header says "
                f"{shard.chunks}; store the paper again"
            )
        return lines

    # ------------------------------------------------------------------ reading

    def _wants_bm25(self, question: str, mode: str) -> bool:
        return mode == "always" or (mode == "english" and looks_english(question))

    def _shards(self) -> dict[str, _Shard]:
        if self._loaded is None:
            self._loaded = self._scan_all()[0]
        return self._loaded

    def _scan_all(self) -> tuple[dict[str, _Shard], list[str], list[str]]:
        """Read-only. The valid shards by document, the stems of the older shards of papers
        that have two, and the stems of vectors without chunks. A shard is valid when its
        .jsonl header is readable and its .npz exists; one that is not is left alone and
        counts as absent. Only a writer deletes anything (``_cleanup``)."""
        shard_dir = self.folder / "shards"
        if not shard_dir.is_dir():
            return {}, [], []
        npz = {p.stem for p in shard_dir.glob("*.npz")}
        found: dict[str, list[tuple[int, _Shard]]] = {}
        for jsonl in sorted(shard_dir.glob("*.jsonl")):
            try:
                with jsonl.open(encoding="utf-8", newline="\n") as f:
                    header = json.loads(f.readline())
                shard = _Shard(
                    header["doc_id"],
                    header["key"],
                    jsonl.stem,
                    header["chunks"],
                    header["dims"],
                    int(header.get("seq", 0)),
                )
                if header["format"] != FORMAT_VERSION or jsonl.stem not in npz:
                    continue
                mtime = jsonl.stat().st_mtime_ns
            except (OSError, ValueError, KeyError, TypeError):
                continue
            found.setdefault(shard.doc_id, []).append((mtime, shard))
        winners: dict[str, _Shard] = {}
        losers: list[str] = []
        for doc_id, candidates in found.items():
            # the write counter says which is newer; the clock only orders shards without one
            candidates.sort(key=lambda c: (c[1].seq, c[0], c[1].stem))
            winners[doc_id] = candidates[-1][1]
            losers += [c[1].stem for c in candidates[:-1]]
        orphans = [s for s in npz if not (shard_dir / f"{s}.jsonl").exists()]
        return winners, losers, orphans

    def _search_dir(self) -> Path | None:
        """``search/``; ``search.old/`` for the instant of a swap in which it is missing."""
        for name in ("search", "search.old"):
            if (self.folder / name / "ids.json").exists():
                return self.folder / name
        return None

    def _read_ids(self) -> tuple[Path, dict[str, Any]] | None:
        where = self._search_dir()
        if where is None:
            return None
        try:
            ids = json.loads((where / "ids.json").read_text())
        except (OSError, ValueError):
            return None
        return (where, ids) if ids.get("format") == FORMAT_VERSION else None

    def _open_search(self) -> _Search | None:
        """The last commit, opened whole: the vectors, the chunk records and the BM25 index of
        one generation. It stays that generation for as long as this object keeps it, however
        many commits other processes make; the next ``close`` or a new object sees the latest.
        A commit that lands while the files are being opened is noticed (``ids.json`` names
        another generation afterwards) and the opening starts over."""
        if self._search is not None:
            return self._search
        for _ in range(OPEN_ATTEMPTS):
            found = self._read_ids()
            if found is None:
                return None
            try:
                state = _Search(*found)
            except FileNotFoundError:  # a swap removed the folder under us
                continue
            after = self._read_ids()
            if after is not None and after[1].get("generation") == found[1].get("generation"):
                self._search = state
                return state
            state.close()
        raise IndexBusyError(
            f"the index {self.folder} kept changing while it was being opened; ask again"
        )


class _Search:
    """A committed ``search/`` folder opened for queries. It reads nothing else."""

    def __init__(self, folder: Path, ids: dict[str, Any]):
        self.folder = folder
        self.rows: int = ids["rows"]
        self.dims: int = ids["dims"]
        self.shards: list[dict[str, Any]] = ids["shards"]
        self.starts = [0]
        for shard in self.shards:
            self.starts.append(self.starts[-1] + shard["chunks"])
        self.dense = np.load(folder / "dense.npy", mmap_mode="r") if self.rows else None
        self._offsets = np.load(folder / "chunks.offsets.npy") if self.rows else None
        self._records = (folder / "chunks.jsonl").open("rb") if self.rows else None
        # Opened with the rest, not at the first English question: a lazily opened index
        # would be the one of a later commit than the vectors and the chunk records.
        self._bm25: tuple[Any, Any] | None = None
        if self.rows:
            import tantivy

            index = tantivy.Index.open(str(folder / "bm25"))
            index.reload()
            self._bm25 = (index, index.searcher())

    def close(self) -> None:
        if self._records is not None:
            self._records.close()
            self._records = None
        self._bm25 = None
        self.dense = None  # drops the memory map: the commit's files are really released
        self._offsets = None

    def query(self, vector: Vector) -> np.ndarray:
        v = np.asarray(vector, dtype=np.float32)
        if v.shape != (self.dims,):
            raise ValueError(
                f"the question vector has shape {v.shape}, the index holds "
                f"{self.dims}-dimensional vectors"
            )
        norm = np.linalg.norm(v)
        if abs(norm - 1.0) <= UNIT_TOLERANCE:  # the embedder's unit vector, as it is
            return v
        return v / (norm + np.float32(1e-12))

    def dense_top(self, query: np.ndarray, depth: int) -> tuple[list[int], np.ndarray]:
        """Rows by cosine similarity, best first (stable), and the score of every row."""
        assert self.dense is not None
        scores = np.empty(self.rows, dtype=np.float32)
        for lo in range(0, self.rows, BLOCK_ROWS):
            scores[lo : lo + BLOCK_ROWS] = (
                self.dense[lo : lo + BLOCK_ROWS].astype(np.float32) @ query
            )
        top = np.argpartition(-scores, depth)[:depth] if self.rows > depth else np.arange(self.rows)
        return [int(r) for r in top[np.argsort(-scores[top], kind="stable")]], scores

    def bm25_top(self, question: str, depth: int) -> list[int]:
        """Rows by BM25 over the words of the question (letters and digits, lowercased)."""
        words = " ".join(_WORDS.findall(question)).lower()
        if not words:
            return []
        assert self._bm25 is not None
        index, searcher = self._bm25
        hits = searcher.search(index.parse_query(words, ["text"]), depth).hits
        return [int(searcher.doc(address)["i"][0]) for _, address in hits]

    def doc_of(self, row: int) -> str:
        # the last shard that starts at or before the row (empty shards share their start)
        return self.shards[bisect_right(self.starts, row) - 1]["doc_id"]

    def chunk(self, row: int) -> Chunk:
        assert self._offsets is not None
        assert self._records is not None
        self._records.seek(int(self._offsets[row]))
        record = json.loads(self._records.readline())
        first, last = record["pages"]
        return Chunk(
            doc_id=self.doc_of(row),
            name=record["name"],
            text=record["text"],
            first_page=first,
            last_page=last,
            is_reference=record["is_reference"],
            start=record["span"][0],
        )
