"""DocumentConverter adapter: LaTeX sources -> Markdown segments via pandoc.

Accepts a ``.tex`` file, an arXiv source archive (tar / tar.gz) or a single gzipped
``.tex`` (old arXiv e-prints). It unpacks safely, picks the main file, inlines every
include in Python (see ``latex_source``: pandoc alone hangs, drops latin-1 files and
reads arbitrary paths), runs pandoc ``--sandbox`` under a wall-clock timeout and a
heap cap, then splits the Markdown into one segment per section (``split_level``).

When pandoc fails, times out or returns suspiciously little text, pylatexenc produces
plain text instead (``PageMethod.LATEX_PLAINTEXT``) and the Conversion says so in its
warnings. Title, authors and abstract come from pandoc's metadata (``$meta-json$``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ...config import LatexConfig
from ...domain.errors import ConversionError
from ...domain.models import PageMethod, SourceMetadata
from ...ports import Conversion, Segment
from .latex_source import (
    approx_text_length,
    document_body,
    find_main,
    flatten,
    match_brace,
    prepare_for_pandoc,
    split_sections,
    unpack,
)

REVISION = 1  # bump when this adapter's output changes for the same pandoc / pylatexenc
# Markdown that keeps $math$, $$display$$, pipe tables and [@citations], without raw
# HTML/TeX or attribute syntax (gfm would drop the citations).
_DISABLED = (
    *("raw_html", "raw_attribute", "raw_tex", "native_divs", "native_spans", "fenced_divs"),
    *("bracketed_spans", "header_attributes", "link_attributes", "inline_code_attributes"),
    *("grid_tables", "multiline_tables", "simple_tables", "implicit_figures", "smart"),
)
MARKDOWN = "markdown" + "".join(f"-{ext}" for ext in _DISABLED)
# --sandbox: pandoc reads nothing but stdin (includes are inlined beforehand).
# --reference-location=block: footnotes stay next to their paragraph, i.e. in its segment.
PANDOC_ARGS = (
    "-f", "latex", "-t", MARKDOWN, "--wrap=none", "--reference-location=block",
    "--sandbox", "-s",
)  # fmt: skip
PANDOC_HEAP = "+RTS -M2g -RTS"  # pandoc's manual: cap the heap for untrusted input
MIN_TEXT_RATIO = 0.2  # pandoc output under 20% of the source's text -> try the fallback
_BODY_MARK = "@@docingest-body@@"
_TEMPLATE = f"$meta-json$\n{_BODY_MARK}\n$body$\n"


def _pylatexenc_version() -> str:
    try:
        return version("pylatexenc")
    except PackageNotFoundError:
        return "missing"


@dataclass
class _Attempt:
    segments: list[Segment]
    metadata: SourceMetadata | None
    warnings: list[str] = field(default_factory=list)

    @property
    def n_chars(self) -> int:
        return sum(len(s.text) for s in self.segments)


class PandocLatexConverter:
    def __init__(self, cfg: LatexConfig | None = None):
        self.cfg = cfg or LatexConfig()

    # ------------------------------------------------------------- identity
    @cached_property
    def pandoc_path(self) -> str | None:
        if self.cfg.pandoc_path:
            return self.cfg.pandoc_path
        try:
            import pypandoc

            return pypandoc.get_pandoc_path()  # the binary bundled by pypandoc-binary
        except (ImportError, OSError):
            return shutil.which("pandoc")

    @cached_property
    def pandoc_version(self) -> str | None:
        if not self.pandoc_path:
            return None
        try:
            out = subprocess.run(
                [self.pandoc_path, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.match(r"pandoc(?:\.exe)?\s+(\S+)", out)
        return m.group(1) if m else None

    @cached_property
    def fingerprint(self) -> str:
        fallback = f"pylatexenc {_pylatexenc_version()}" if self.cfg.fallback else "no fallback"
        return (
            f"pandoc-latex r{REVISION} | pandoc {self.pandoc_version or 'unavailable'} | "
            f"{' '.join(PANDOC_ARGS)} | split_level={self.cfg.split_level} | {fallback}"
        )

    @property
    def engine(self) -> str:
        return f"pandoc {self.pandoc_version or 'unknown'}"

    # ------------------------------------------------------------- convert
    def convert(self, path: Path) -> Conversion:
        max_bytes = self.cfg.max_archive_mb * 2**20
        with tempfile.TemporaryDirectory(prefix="docingest-latex-") as tmp:
            work = Path(tmp)
            try:
                src = unpack(path, work / "src", max_bytes=max_bytes)
                warnings = list(src.warnings)
                main = src.main
                if main is None:
                    main, why = find_main(src.root)
                    warnings += why
                flat = flatten(main, src.root, max_chars=max_bytes)
            except OSError as e:  # unreadable file in the source tree, disk full, ...
                raise ConversionError(f"cannot read LaTeX source {path.name}: {e}") from e
            warnings += flat.warnings
            latex, why = prepare_for_pandoc(flat.text)
            warnings += why
            return self._convert_latex(latex, work, warnings, path.name)

    def _convert_latex(self, latex: str, work: Path, warnings: list[str], name: str) -> Conversion:
        attempt, problem = self._pandoc(latex, work)
        if attempt is not None:
            expected = approx_text_length(latex)
            if attempt.n_chars == 0:
                problem = "pandoc produced no text"
            elif attempt.n_chars < MIN_TEXT_RATIO * expected:
                problem = f"pandoc kept {attempt.n_chars} chars of ~{expected} in the source"
            else:
                return _conversion(attempt, PageMethod.LATEX, self.engine, warnings)
        assert problem is not None
        got = attempt.n_chars if attempt else 0
        if self.cfg.fallback:
            try:
                plain = self._plaintext(latex)
            except Exception as e:  # pylatexenc bugs of any kind mean "no fallback"
                plain = None
                problem += f"; pylatexenc failed: {e}"
            if plain is not None and plain.n_chars > got:
                engine = f"pylatexenc {_pylatexenc_version()}"
                note = f"{problem}; used the pylatexenc plain-text fallback"
                return _conversion(plain, PageMethod.LATEX_PLAINTEXT, engine, [*warnings, note])
        if attempt is not None and got:
            return _conversion(attempt, PageMethod.LATEX, self.engine, [*warnings, problem])
        context = f" ({'; '.join(warnings)})" if warnings else ""
        raise ConversionError(f"{name}: {problem}{context}")

    # --------------------------------------------------------------- pandoc
    def _pandoc(self, latex: str, work: Path) -> tuple[_Attempt | None, str | None]:
        if not self.pandoc_path:
            return None, "pandoc not found"
        template = work / "docingest-template.md"
        template.write_text(_TEMPLATE)
        cmd = [self.pandoc_path, *PANDOC_HEAP.split(), *PANDOC_ARGS, f"--template={template}"]
        try:
            proc = subprocess.run(
                cmd,
                input=latex.encode("utf-8"),
                capture_output=True,
                cwd=work,
                timeout=self.cfg.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, f"pandoc timed out after {self.cfg.timeout_s}s"
        except OSError as e:
            return None, f"pandoc could not run: {e}"
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            detail = " ".join(stderr.split())[:300]
            return None, f"pandoc exited with code {proc.returncode}: {detail}"
        meta_json, _, body = proc.stdout.decode("utf-8", errors="replace").partition(_BODY_MARK)
        try:
            meta = json.loads(meta_json)
        except ValueError:
            meta = {}
        metadata = markdown_metadata(meta if isinstance(meta, dict) else {})
        parts = split_markdown(body, self.cfg.split_level)
        abstract = metadata.abstract if metadata else None
        return _Attempt(assemble(parts, abstract), metadata, pandoc_warnings(stderr)), None

    # ------------------------------------------------------------- fallback
    def _plaintext(self, latex: str) -> _Attempt:
        from pylatexenc.latex2text import LatexNodes2Text

        l2t = LatexNodes2Text(math_mode="verbatim")
        crude: list[str] = []

        def text(fragment: str) -> str:
            fragment = _plain_friendly(fragment)
            try:
                out = l2t.latex_to_text(fragment)
            except Exception:  # pylatexenc has spec bugs (e.g. \href in a footnote)
                crude.append(fragment)
                out = _crude_text(fragment)
            return re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in out.splitlines()))

        title = _without_thanks(_braced_arg(latex, "title"))
        authors = _without_thanks(_braced_arg(latex, "author"))
        body = document_body(latex)
        abstract = None
        if m := re.search(r"\\begin\s*\{abstract\}(.*?)\\end\s*\{abstract\}", body, re.S):
            abstract = text(m.group(1)).strip() or None
            body = body[: m.start()] + body[m.end() :]
        parts = []
        for level, head, content in split_sections(body, self.cfg.split_level):
            heading = inline_text(text(head)) if head is not None else None
            parts.append(
                (level, heading, f"{'#' * level} {heading}" if heading else "", text(content))
            )
        names = []
        if authors:
            names = author_names([text(a) for a in re.split(r"\\and(?![a-zA-Z@])", authors)])
        metadata = _metadata(inline_text(text(title)) if title else None, names, abstract)
        notes = (
            [f"pylatexenc failed on {len(crude)} fragment(s); kept their bare text"]
            if crude
            else []
        )
        return _Attempt(assemble(parts, abstract), metadata, notes)


def _conversion(
    attempt: _Attempt, method: PageMethod, engine: str, warnings: list[str]
) -> Conversion:
    meta = attempt.metadata
    return Conversion(
        segments=attempt.segments,
        method=method,
        engine=engine,
        title=meta.title if meta else None,
        metadata=meta,
        warnings=[*warnings, *attempt.warnings],
    )


# ------------------------------------------------------------ pure helpers
_ATX = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_DISPLAY_MATH = re.compile(r"(?<!\\)\$\$")
_FOOTNOTE_REF = re.compile(r"\[\^[^\]]*\]")


def inline_text(md: str) -> str:
    """One-line plain-ish text of a Markdown inline string (titles, headings)."""
    md = _FOOTNOTE_REF.sub("", md)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)  # [text](#label) -> text
    md = md.replace("\\\n", " ")
    return " ".join(md.split())


def split_markdown(md: str, max_level: int) -> list[tuple[int, str | None, str, str]]:
    """Cut Markdown at ATX headings of level <= ``max_level``.

    Returns ``(level, title, heading line, content)``; content before the first heading
    comes first (level 0, no title). ``#`` lines inside code fences or ``$$`` display
    math are content, not headings.
    """
    max_level = max(1, max_level)
    parts: list[tuple[int, str | None, str, list[str]]] = [(0, None, "", [])]
    fence: re.Pattern[str] | None = None
    in_math = False
    for line in md.split("\n"):
        if fence is not None:
            if fence.match(line):
                fence = None
        elif not in_math and (f := _FENCE.match(line)):
            char, n = f.group(1)[0], len(f.group(1))
            fence = re.compile(rf"^[ \t]{{0,3}}{re.escape(char)}{{{n},}}[ \t]*$")
        elif not in_math and (h := _ATX.match(line)) and len(h.group(1)) <= max_level:
            parts.append((len(h.group(1)), inline_text(h.group(2)), line, []))
            continue
        elif len(_DISPLAY_MATH.findall(line)) % 2:
            in_math = not in_math
        parts[-1][3].append(line)
    return [(lvl, title, head, "\n".join(lines)) for lvl, title, head, lines in parts]


def assemble(
    parts: Sequence[tuple[int, str | None, str, str]], abstract: str | None = None
) -> list[Segment]:
    """Segments titled with their heading path ("Model > Attention").

    A heading followed directly by a sub-heading is glued onto the next segment instead
    of becoming a segment of its own; the abstract, if any, comes first.
    """
    segments = [Segment(f"# Abstract\n\n{abstract.strip()}", "Abstract")] if abstract else []
    path: dict[int, str] = {}
    carry: list[str] = []
    carry_title: str | None = None
    for level, title, heading, content in parts:
        full = None
        if title is not None:
            path = {k: v for k, v in path.items() if k < level}
            path[level] = title
            full = " > ".join(path[k] for k in sorted(path) if path[k])
        body = re.sub(r"\n{3,}", "\n\n", content.strip())
        if heading and not body:
            carry.append(heading)
            carry_title = full
            continue
        text = "\n\n".join(x for x in (*carry, heading, body) if x)
        carry = []
        if text:
            segments.append(Segment(text, full or None))
    if carry:
        segments.append(Segment("\n\n".join(carry), carry_title))
    return segments


def _meta_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        return "\n\n".join(t for v in value if (t := _meta_text(v))) or None
    return None


_ORG = re.compile(
    r"(?i)\b(univ\w*|institut\w*|laborator\w*|labs?|college|school|department|dept|faculty|"
    r"academy|cent(?:er|re)|hospital|inc|ltd|llc|corp\w*|gmbh|research|foundation|google|"
    r"microsoft|deepmind|meta|facebook|nvidia|amazon|ibm|apple|brain|email|correspondence)\b"
)


def _looks_like_name(s: str) -> bool:
    words = s.split()
    return 0 < len(words) <= 6 and len(s) <= 60 and s[0].isupper() and ":" not in s


def author_names(value: object) -> list[str]:
    """Best-effort author names from pandoc's ``author`` metadata (or plain text).

    LaTeX author blocks mix names, affiliation markers, affiliations and emails; keep
    the names: the first line of each entry, plus following lines until one looks
    like an affiliation or contact. Inline math (``$^{1}$``) separates names.
    """
    entries = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        lines = [ln.strip() for ln in re.split(r"\\\n|\n", entry)]
        for i, line in enumerate(ln for ln in lines if ln):
            if line.startswith(("$", "^")) or any(c in line for c in "@`") or "http" in line:
                break
            if i and _ORG.search(line):
                break
            bare = re.sub(r"\$[^$]*\$", ",", _FOOTNOTE_REF.sub("", line))
            bare = re.sub(r"[*†‡§¶∗⋆\d^{}\\]", "", bare)
            for part in re.split(r",|;|&|\band\b", bare):
                name = " ".join(part.split()).strip(" .")
                if _looks_like_name(name):
                    names.append(name)
    return list(dict.fromkeys(names))


def _metadata(title: str | None, authors: list[str], abstract: str | None) -> SourceMetadata | None:
    if not (title or authors or abstract):
        return None
    return SourceMetadata(title=title or None, authors=authors, abstract=abstract)


def markdown_metadata(meta: dict) -> SourceMetadata | None:
    """SourceMetadata from pandoc's ``$meta-json$`` (values already Markdown)."""
    title = _meta_text(meta.get("title"))
    abstract = _meta_text(meta.get("abstract"))
    authors = author_names(meta.get("author"))
    return _metadata(inline_text(title) if title else None, authors, abstract)


def pandoc_warnings(stderr: str) -> list[str]:
    """pandoc's [WARNING] lines, summarized into one entry (they can number hundreds)."""
    msgs = [
        ln[len("[WARNING]") :].strip() for ln in stderr.splitlines() if ln.startswith("[WARNING]")
    ]
    if not msgs:
        return []
    kinds = list(dict.fromkeys(re.sub(r"\s*at line \d+ column \d+", "", m) for m in msgs))
    sample = "; ".join(kinds[:3])[:300]
    return [f"pandoc: {len(msgs)} warning(s), e.g. {sample}"]


_CITE = re.compile(
    r"\\(?:[cC]ite[a-zA-Z]*|[pP]arencite|[tT]extcite|[aA]utocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}"
    r"\{([^{}]*)\}"
)
_REF = re.compile(r"\\(?:eq|auto|page|name|c|C)?ref\*?\s*\{([^{}]*)\}")


def _escape_url(url: str) -> str:
    return re.sub(r"([%#_&$])", r"\\\1", url.strip())


def _plain_friendly(latex: str) -> str:
    """Rewrites so pylatexenc's text keeps citations, references and links readable."""
    latex = re.sub(
        r"\\href\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
        lambda m: (
            f"{m[2]} ({_escape_url(m[1])})" if m[2].strip() != m[1].strip() else _escape_url(m[1])
        ),
        latex,
    )
    latex = re.sub(r"\\url\s*\{([^{}]*)\}", lambda m: _escape_url(m[1]), latex)
    latex = re.sub(r"\\iffalse\b.*?\\fi\b", "", latex, flags=re.S)
    latex = re.sub(r"\\begin\{comment\}.*?\\end\{comment\}", "", latex, flags=re.S)
    latex = re.sub(r"\\(?:maketitle|tableofcontents)\b", "", latex)
    latex = _CITE.sub(
        lambda m: "[" + "; ".join(f"@{k.strip()}" for k in m[1].split(",")) + "]", latex
    )
    return _REF.sub(lambda m: f"[{m[1].strip()}]", latex)


def _crude_text(latex: str) -> str:
    """Last resort when pylatexenc raises: drop control words and braces, keep the words."""
    latex = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", "\n", latex)
    latex = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?", " ", latex)
    latex = re.sub(r"\\(.)", r"\1", latex)
    return re.sub(r"[ \t]+", " ", latex.replace("{", "").replace("}", ""))


def _without_thanks(latex: str | None) -> str | None:
    """Drop ``\\thanks{...}`` footnotes (pylatexenc would glue them onto the name)."""
    while latex and (m := re.search(r"\\thanks\s*(?=\{)", latex)):
        end = match_brace(latex, m.end())
        latex = latex[: m.start()] + (latex[end:] if end else "")
    return latex


def _braced_arg(latex: str, command: str) -> str | None:
    """The mandatory argument of the first ``\\command[...]{...}``."""
    m = re.search(rf"\\{command}(?![a-zA-Z@])\s*(?:\[[^\]]*\])?\s*(?=\{{)", latex)
    if m is None:
        return None
    end = match_brace(latex, m.end())
    return latex[m.end() + 1 : end - 1] if end else None
