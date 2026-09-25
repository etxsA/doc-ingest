"""DocumentStore adapter: content-addressed directories on the local filesystem.

    <root>/<sha256[:16]>/                         canonical: complete, default-option runs
        document.md, manifest.json
    <root>/_variants/<id>-<config>-p<N|all>/      --max-pages / --ocr-all runs
    <root>/index.json                             catalog of canonical documents

Writes are atomic (temp file + rename, normal permissions); an unreadable manifest
counts as a cache miss; a partial run never replaces a complete result.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ...domain.models import DocumentManifest
from ...ports import StoredDocument

VARIANTS_DIR = "_variants"
_UMASK = os.umask(0)
os.umask(_UMASK)  # read once at import; os.umask is process-global


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o666 & ~_UMASK)  # mkstemp creates 0600; match a normal write
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load(path: Path) -> DocumentManifest | None:
    try:
        return DocumentManifest.model_validate_json(path.read_text())
    except (OSError, ValueError):  # missing, truncated, or from an older schema -> cache miss
        return None


class FilesystemStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _canonical(self, doc_id: str) -> Path:
        return self.root / doc_id[:16]

    def _variant(self, doc_id: str, config_hash: str, max_pages: int | None) -> Path:
        tag = "all" if max_pages is None else str(max_pages)
        return self.root / VARIANTS_DIR / f"{doc_id[:16]}-{config_hash}-p{tag}"

    def lookup(
        self, doc_id: str, config_hash: str, *, max_pages: int | None, ocr_all: bool
    ) -> StoredDocument | None:
        # A complete canonical result satisfies default and --max-pages requests alike.
        canonical = self._canonical(doc_id)
        m = _load(canonical / "manifest.json")
        if m and m.config_hash == config_hash and m.complete and m.ocr_all == ocr_all:
            return StoredDocument(m, str(canonical), canonical=True)
        if max_pages is not None or ocr_all:
            variant = self._variant(doc_id, config_hash, max_pages)
            m = _load(variant / "manifest.json")
            if m and m.config_hash == config_hash and m.max_pages == max_pages:
                return StoredDocument(m, str(variant), canonical=False)
        return None

    def save(self, manifest: DocumentManifest, markdown: str) -> StoredDocument:
        canonical = manifest.complete and not manifest.ocr_all
        out = (
            self._canonical(manifest.doc_id)
            if canonical
            else self._variant(manifest.doc_id, manifest.config_hash, manifest.max_pages)
        )
        out.mkdir(parents=True, exist_ok=True)
        _atomic_write(out / "document.md", markdown)
        _atomic_write(out / "manifest.json", manifest.model_dump_json(indent=2))
        if canonical:
            self._index(manifest)
        return StoredDocument(manifest, str(out), canonical=canonical)

    def markdown(self, doc: StoredDocument) -> str:
        return (Path(doc.location) / "document.md").read_text()

    def corpus(self) -> tuple[list[StoredDocument], list[str]]:
        best: dict[str, StoredDocument] = {}
        warnings: list[str] = []
        canon = sorted(self.root.glob("*/manifest.json"))
        variants = sorted((self.root / VARIANTS_DIR).glob("*/manifest.json"))
        for path in [*canon, *variants]:
            m = _load(path)
            if m is None:
                warnings.append(f"skipped unreadable manifest {path} (re-run `docingest ingest`)")
                continue
            is_canon = VARIANTS_DIR not in path.parts
            seen = best.get(m.doc_id)
            if seen is None or (not seen.canonical and m.n_pages > seen.manifest.n_pages):
                best[m.doc_id] = StoredDocument(m, str(path.parent), canonical=is_canon)
        for d in best.values():
            if not d.canonical:
                m = d.manifest
                warnings.append(
                    f"{m.source_name}: only a partial run exists ({m.n_pages}/{m.source_pages} pages)"
                )
        return list(best.values()), warnings

    def _index(self, m: DocumentManifest) -> None:
        index = self.root / "index.json"
        try:
            catalog = json.loads(index.read_text())
        except (OSError, ValueError):
            catalog = {}
        catalog[m.doc_id] = {
            "source_name": m.source_name,
            "title": m.title,
            "kind": m.source_kind.value,
            "pages": m.n_pages,
            "ocr_pages": m.ocr_pages,
            "dir": m.doc_id[:16],
            "citation": m.citation(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(index, json.dumps(catalog, indent=2))
