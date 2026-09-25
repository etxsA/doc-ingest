"""DocumentConverter adapter: LaTeX sources -> Markdown segments via pandoc.

Accepts a ``.tex`` file, an arXiv source archive (tar / tar.gz) or a single gzipped
``.tex`` (old arXiv e-prints). It unpacks safely, picks the main file, inlines every
include in Python (see ``latex_source``: pandoc alone hangs, drops latin-1 files and
reads arbitrary paths), runs pandoc ``--sandbox`` under a wall-clock timeout and a
heap cap, then splits the Markdown into one segment per section (``split_level``).

When pandoc fails, times out or returns suspiciously little text, pylatexenc produces
plain text instead (``PageMethod.LATEX_PLAINTEXT``) and the Conversion says so in its
warnings; it is also marked ``degraded`` when the cause was the machine (a timeout, a
crash, no pandoc binary) rather than the document, so it is not cached as the result.
Title, authors and abstract come from pandoc's metadata (``$meta-json$``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache, cached_property
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ...config import LatexConfig
from ...domain.errors import ConversionError
from ...domain.models import PageMethod, SourceMetadata
from ...ports import Conversion, Segment
from .latex_source import (
    FI,
    IFFALSE,
    approx_text_length,
    document_body,
    find_main,
    flatten,
    match_brace,
    prepare_for_pandoc,
    regions,
    split_sections,
    sub_regions,
    unpack,
    verbatim_parts,
)

REVISION = 2  # bump when this adapter's output changes for the same pandoc / pylatexenc
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


@dataclass(frozen=True)
class _Failure:
    reason: str
    environmental: bool = False  # the machine, not the document: a later run may succeed


def _environmental(returncode: int) -> bool:
    """pandoc's own error exits (1-99; 64 is a parse error, 91 a macro loop) are about
    the document and recur on every run. A signal (negative: the OOM killer, a kill) or
    a GHC runtime abort (251: heap exhausted under ``PANDOC_HEAP``) is the machine's."""
    return not 0 < returncode < 100


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
        attempt, failure = self._pandoc(latex, work)
        if attempt is not None:
            expected = approx_text_length(latex)
            if attempt.n_chars == 0:
                failure = _Failure("pandoc produced no text")
            elif attempt.n_chars < MIN_TEXT_RATIO * expected:
                failure = _Failure(
                    f"pandoc kept {attempt.n_chars} chars of ~{expected} in the source"
                )
            else:
                return _conversion(attempt, PageMethod.LATEX, self.engine, warnings)
        assert failure is not None
        problem = failure.reason
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
                return _conversion(
                    plain,
                    PageMethod.LATEX_PLAINTEXT,
                    engine,
                    [*warnings, note],
                    degraded=failure.environmental,
                )
        if attempt is not None and got:
            return _conversion(attempt, PageMethod.LATEX, self.engine, [*warnings, problem])
        context = f" ({'; '.join(warnings)})" if warnings else ""
        raise ConversionError(f"{name}: {problem}{context}")

    # --------------------------------------------------------------- pandoc
    def _pandoc(self, latex: str, work: Path) -> tuple[_Attempt | None, _Failure | None]:
        if not self.pandoc_path:
            return None, _Failure("pandoc not found", environmental=True)
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
        except subprocess.TimeoutExpired:  # a loaded machine, or a document pandoc loops on
            return None, _Failure(
                f"pandoc timed out after {self.cfg.timeout_s}s", environmental=True
            )
        except OSError as e:
            return None, _Failure(f"pandoc could not run: {e}", environmental=True)
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            detail = " ".join(stderr.split())[:300]
            reason = f"pandoc exited with code {proc.returncode}: {detail}"
            return None, _Failure(reason, environmental=_environmental(proc.returncode))
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

        l2t = LatexNodes2Text(latex_context=_text_context(), math_mode="verbatim")
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
        if found := next(regions(body, _ABSTRACT_BEGIN, _ABSTRACT_END), None):
            begin, end = found
            abstract = text(body[begin.end() : end.start()]).strip() or None
            body = body[: begin.start()] + body[end.end() :]
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
    attempt: _Attempt,
    method: PageMethod,
    engine: str,
    warnings: list[str],
    *,
    degraded: bool = False,
) -> Conversion:
    meta = attempt.metadata
    return Conversion(
        segments=attempt.segments,
        method=method,
        engine=engine,
        title=meta.title if meta else None,
        metadata=meta,
        warnings=[*warnings, *attempt.warnings],
        degraded=degraded,
    )


# ------------------------------------------------------------ pure helpers
_ATX = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
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
    comes first (level 0, no title). As in pandoc's reader (``blank_before_header``), a
    ``#`` line is a heading only at the start or after a blank line, and never inside a
    code fence. pandoc writes every heading that way, and ``$$`` display math cannot
    hold a blank line (TeX forbids it), so a ``#`` line in math is never taken for one.
    Counting ``$$`` per line does not work: pandoc writes adjacent inline maths
    (``$3$$\\times 10^{-4}$``) with a single ``$$``.
    """
    max_level = max(1, max_level)
    parts: list[tuple[int, str | None, str, list[str]]] = [(0, None, "", [])]
    fence: re.Pattern[str] | None = None
    blank = True  # the start of the document counts as a blank line
    for line in md.split("\n"):
        if fence is not None:
            if fence.match(line):
                fence = None
        elif f := _FENCE.match(line):
            char, n = f.group(1)[0], len(f.group(1))
            fence = re.compile(rf"^[ \t]{{0,3}}{re.escape(char)}{{{n},}}[ \t]*$")
        elif blank and (h := _ATX.match(line)) and len(h.group(1)) <= max_level:
            parts.append((len(h.group(1)), inline_text(h.group(2)), line, []))
            blank = False
            continue
        parts[-1][3].append(line)
        blank = not line.strip()
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


_ABSTRACT_BEGIN = re.compile(r"\\begin\s*\{abstract\}")
_ABSTRACT_END = re.compile(r"\\end\s*\{abstract\}")
_VERB_MARK = "\ue000"  # private-use character: the delimiter of a rewritten \verb
_MINTED_ARGS = re.compile(r"\A[ \t]*(?:\[[^\]\n]*\])?[ \t]*\{[^{}\n]*\}")  # [opts]{language}
_LISTING_OPTS = re.compile(r"\A[ \t]*\[[^\]\n]*\]")  # lstlisting / fancyvrb [options]


def _verbatim_text(node: Any) -> str:
    """The text of a ``\\verb`` or ``verbatim`` node, exactly as written."""
    return getattr(node.nodeargd, "verbatim_text", "") or ""


@cache
def _text_context() -> Any:
    """pylatexenc's defaults, plus the text they silently drop: the argument of
    ``\\texttt``, ``\\textsf``, ``\\textup``, ``\\textmd``, ``\\mbox`` and ``\\verb`` and
    the body of ``verbatim`` (model, dataset and code names vanished from the text)."""
    from pylatexenc.latex2text import (
        EnvironmentTextSpec,
        MacroTextSpec,
        get_default_latex_context_db,
    )

    db = get_default_latex_context_db()
    db.add_context_category(
        "docingest",
        prepend=True,
        macros=[
            *(
                MacroTextSpec(name, discard=False)  # discard=None drops the argument
                for name in ("texttt", "textsf", "textup", "textmd", "mbox")
            ),
            MacroTextSpec("verb", simplify_repl=_verbatim_text),
        ],
        environments=[EnvironmentTextSpec("verbatim", simplify_repl=_verbatim_text)],
    )
    return db


def _plain_verbatim(latex: str) -> str:
    """Every verbatim construct as the two pylatexenc reads verbatim, ``\\verb`` and the
    ``verbatim`` environment (it takes the ``%`` in ``\\lstinline!n % 2!`` for a comment
    and prints the language of ``minted``); comment and filecontents bodies dropped."""
    parts: list[str] = []
    pos = 0
    for v in verbatim_parts(latex):
        parts.append(latex[pos : v.start])
        pos = v.end
        if v.env is None:
            parts.append(f"\\verb{_VERB_MARK}{v.body.replace(_VERB_MARK, '')}{_VERB_MARK}")
        elif v.env != "comment" and not v.env.startswith("filecontents"):
            body = v.body
            if v.env == "minted":
                body = _MINTED_ARGS.sub("", body)
            elif not v.env.startswith("verbatim"):
                body = _LISTING_OPTS.sub("", body)
            parts.append(f"\\begin{{verbatim}}{body}\\end{{verbatim}}")
    parts.append(latex[pos:])
    return "".join(parts)


def _plain_friendly(latex: str) -> str:
    """Rewrites so pylatexenc's text keeps citations, references, links and verbatim
    text readable."""
    latex = _plain_verbatim(latex)
    latex = re.sub(
        r"\\href\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
        lambda m: (
            f"{m[2]} ({_escape_url(m[1])})" if m[2].strip() != m[1].strip() else _escape_url(m[1])
        ),
        latex,
    )
    latex = re.sub(r"\\url\s*\{([^{}]*)\}", lambda m: _escape_url(m[1]), latex)
    latex = sub_regions(latex, IFFALSE, FI, lambda _: "")  # linear, unlike \\iffalse.*?\\fi
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
