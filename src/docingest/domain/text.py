"""Pure text functions: text-layer cleanup, quality signals, Markdown (de)serialization."""

from __future__ import annotations

import re

from .models import DocumentManifest

HYPHEN_MARK = "\x02"  # pdfium replaces a line-end hyphen with this and joins the lines

_CID = re.compile(r"\(cid:\d+\)")
_SOFT_HYPHEN = re.compile(rf"(\w+){HYPHEN_MARK}(\w+)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PAGE_MARKER = re.compile(r"^<!-- page (\d+) \| method=(\w+) -->$", re.MULTILINE)


def garbage_ratio(text: str) -> float:
    """Share of characters that indicate a broken text layer."""
    stripped = "".join(text.split())
    if not stripped:
        return 0.0
    bad = stripped.count("�") + sum(
        1
        for c in stripped
        # control / private-use; U+0002 is pdfium's soft-hyphen marker, not garbage
        if (ord(c) < 32 and c != HYPHEN_MARK) or 0xE000 <= ord(c) <= 0xF8FF
    )
    bad += sum(len(m) for m in _CID.findall(text))
    return bad / len(stripped)


def alpha_ratio(text: str) -> float:
    """Share of letters among non-space chars (olmOCR flags < 0.5 as bad text)."""
    stripped = "".join(text.split())
    return sum(c.isalpha() for c in stripped) / len(stripped) if stripped else 0.0


def text_vocabulary(*texts: str) -> set[str]:
    return {w.lower() for t in texts for w in re.findall(r"\w+", t.replace(HYPHEN_MARK, " "))}


def clean_text_layer(text: str, vocab: set[str] | None = None) -> str:
    """Normalize a PDF text layer.

    pdfium turns a line-end hyphen into U+0002 and joins the lines, for both real
    hyphenation ("transduc-tion") and compounds ("sequence-aligned"). Join the two
    halves only when the joined word occurs elsewhere in the document; otherwise
    keep the hyphen, which never loses information.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    vocab = text_vocabulary(text) if vocab is None else vocab

    def _join(m: re.Match[str]) -> str:
        a, b = m.group(1), m.group(2)
        return a + b if (a + b).lower() in vocab else f"{a}-{b}"

    text = _SOFT_HYPHEN.sub(_join, text).replace(HYPHEN_MARK, "-")
    # Remaining "-\n" breaks are numbers / identifiers (2019-2020, Qwen-2.5-VL): keep the hyphen.
    text = re.sub(r"(?<=\w)-\n(?=\w)", "-", text)
    text = _CONTROL.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_markdown(manifest: DocumentManifest, texts: list[str]) -> str:
    parts = [f"# {manifest.title}\n" if manifest.title else ""]
    for rec, text in zip(manifest.pages, texts, strict=True):
        parts.append(f"<!-- page {rec.index + 1} | method={rec.method.value} -->\n{text}\n")
    return "\n".join(parts).strip() + "\n"


def split_pages(markdown: str) -> dict[int, str]:
    """Inverse of :func:`render_markdown`: {1-based page number: page text}."""
    marks = list(_PAGE_MARKER.finditer(markdown))
    pages = {}
    for m, nxt in zip(marks, [*marks[1:], None], strict=True):
        end = nxt.start() if nxt else len(markdown)
        pages[int(m.group(1))] = markdown[m.end() : end].strip()
    return pages
