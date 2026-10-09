"""``chunk_pages`` gives the same chunks as PaperQA2's ``chunk_pdf``, as the adapter calls it.

The one difference is a document without pages: ``chunk_pdf`` raises, ``chunk_pages`` returns
no chunks. PaperQA2 has no offsets, so ``start`` is checked against the joined page texts.
"""

from pathlib import Path

import pytest

pytest.importorskip("paperqa")

from fakes import FakeOcr
from hypothesis import given, settings
from hypothesis import strategies as st
from paperqa.readers import chunk_pdf
from paperqa.types import Doc, ParsedMetadata, ParsedText
from test_pandoc_latex import needs_pandoc, pack, paper_files

from docingest.bootstrap import Container
from docingest.domain.chunking import CHUNK_CHARS, OVERLAP, chunk_pages
from docingest.domain.text import split_pages

DOC_ID = "0123456789abcdef" * 4
REPO_DOCS = [
    Path(__file__).parents[2] / "README.md",
    Path(__file__).parents[2] / "CONTRIBUTING.md",
    Path(__file__).parents[2] / "docs" / "architecture.md",
]


def paperqa_chunks(pages: dict[int, str], chunk_chars: int, overlap: int):
    """What ``PaperQAAnswerer`` does with a document's pages."""
    parsed = ParsedText(
        content={str(n): text + "\n\n" for n, text in pages.items()},
        metadata=ParsedMetadata(
            parsing_libraries=["docingest"], total_parsed_text_length=sum(map(len, pages.values()))
        ),
    )
    doc = Doc(docname=DOC_ID[:16], dockey=DOC_ID, citation="c")
    return [
        (t.name, t.text) for t in chunk_pdf(parsed, doc, chunk_chars=chunk_chars, overlap=overlap)
    ]


def assert_same(pages: dict[int, str], chunk_chars: int = CHUNK_CHARS, overlap: int = OVERLAP):
    mine = chunk_pages(DOC_ID, pages, chunk_chars=chunk_chars, overlap=overlap)
    assert [(c.name, c.text) for c in mine] == paperqa_chunks(pages, chunk_chars, overlap)
    joined = "".join(text + "\n\n" for text in pages.values())
    assert all(joined[c.start : c.start + len(c.text)] == c.text for c in mine)


def paginate(text: str, page_chars: int) -> dict[int, str]:
    """Pages of about ``page_chars``, cut at paragraph ends, as a document.md would hold."""
    pages: dict[int, str] = {}
    current = ""
    for paragraph in text.split("\n\n"):
        if current and len(current) + len(paragraph) > page_chars:
            pages[len(pages) + 1] = current.strip()
            current = ""
        current += paragraph + "\n\n"
    pages[len(pages) + 1] = current.strip()
    markdown = "".join(f"<!-- page {n} | method=text_layer -->\n{t}\n\n" for n, t in pages.items())
    return split_pages(markdown)


@pytest.mark.parametrize("doc", REPO_DOCS, ids=lambda p: p.name)
@pytest.mark.parametrize("page_chars", [400, 2500, 30_000])
def test_the_repositorys_own_documents_chunk_alike(doc, page_chars):
    pages = paginate(doc.read_text(), page_chars)
    assert len(pages) > 1 or page_chars == 30_000
    assert_same(pages)


@pytest.mark.parametrize(("chunk_chars", "overlap"), [(900, 100), (300, 0), (500, 499), (64, 8)])
def test_other_chunk_settings_chunk_alike(chunk_chars, overlap):
    assert_same(paginate(REPO_DOCS[0].read_text(), 1500), chunk_chars, overlap)


@needs_pandoc
def test_an_ingested_latex_paper_chunks_alike(cfg, tmp_path):
    svc = Container(cfg, log=lambda _: None, overrides={"ocr": FakeOcr()}).ingest
    doc = svc.ingest(pack(tmp_path / "2401.00001.tar.gz", paper_files()))
    pages = split_pages(svc.store.markdown(doc))
    assert len(pages) == 7
    assert_same(pages)
    assert_same(pages, chunk_chars=120, overlap=20)


@settings(max_examples=300, deadline=None)
@given(
    pages=st.lists(st.text(max_size=700), min_size=1, max_size=8),
    chunk_chars=st.integers(min_value=2, max_value=400),
    data=st.data(),
)
def test_any_pages_chunk_alike(pages, chunk_chars, data):
    overlap = data.draw(st.integers(min_value=0, max_value=chunk_chars - 1))
    assert_same(dict(enumerate(pages, 1)), chunk_chars, overlap)
