"""Turn a LaTeX source (a ``.tex`` file or an arXiv archive) into one self-contained string.

pandoc is not trusted with the file system. It resolves ``\\input`` against its CWD,
silently drops includes that are not UTF-8, hangs forever on a file that inputs itself
or on a self-referential macro (conference ``.sty`` files do
``\\renewcommand{\\small}{\\@setfontsize\\small...}``), and reads any path it is given
(``\\lstinputlisting{/etc/passwd}`` ends up in the Markdown). So the converter unpacks
archives safely, picks the main file the way arXiv does, and inlines every include
here; pandoc then runs with ``--sandbox`` on a single document.

Pure functions over paths and strings: no pandoc, no pylatexenc.
"""

from __future__ import annotations

import codecs
import gzip
import io
import json
import re
import tarfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.errors import ConversionError

TEX_SUFFIXES = frozenset({".tex", ".ltx"})
MAIN_NAMES = ("main", "ms", "paper", "article")  # tie-break between several main files
MAX_DEPTH = 20  # nested \input levels
MAX_MEMBERS = 20_000  # files in one archive
MAX_TAR_HEADER = 2**20  # bytes of headers (pax, GNU long name) for one archive member
MAX_MACRO_BODY = 20_000  # chars; longer "definitions" are unbalanced braces, not macros

# Never needed to produce text: not extracted (faster, and a smaller zip-bomb surface).
_MEDIA_SUFFIXES = frozenset(
    {
        *(".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".svg"),
        *(".eps", ".ps", ".pdf", ".ai", ".psd", ".mp4", ".mov", ".avi"),
        *(".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z"),
        *(".npy", ".npz", ".pkl", ".pt", ".pth", ".ckpt", ".h5", ".hdf5", ".bin", ".mat"),
        *(".xlsx", ".docx", ".pptx"),
    }
)
_GZIP_MAGIC = b"\x1f\x8b"

# Environments whose content LaTeX reads verbatim: no comments, no includes inside.
_VERBATIM_ENVS = r"verbatim\*?|Verbatim\*?|BVerbatim|LVerbatim|lstlisting|minted|comment"
_NOT_LETTER = r"(?![a-zA-Z@])"

# Commands pandoc must see as themselves; a redefinition (a conference style's
# \renewcommand{\section}{\@startsection...}) hides headings, titles or authors.
STRUCTURAL = frozenset(
    {
        *("part", "chapter", "section", "subsection", "subsubsection", "paragraph"),
        *("subparagraph", "maketitle", "title", "author", "and", "And", "AND", "thanks"),
        *("date", "abstract", "footnote", "caption", "label", "ref", "cite", "item"),
        *("begin", "end", "emph", "textbf", "textit", "input", "include", "document"),
        *("appendix", "bibliography"),
    }
)


# ------------------------------------------------------------------ decoding
def _legacy_char(byte: int) -> str:
    try:
        return bytes([byte]).decode("cp1252")
    except UnicodeDecodeError:  # 0x81, 0x8d, 0x8f, 0x90, 0x9d: undefined in cp1252
        return chr(byte)  # latin-1


_LEGACY = [_legacy_char(b) for b in range(256)]


def _legacy_bytes(err: UnicodeError) -> tuple[str, int]:
    """Decode error handler: the bytes that are not UTF-8 are cp1252 (else latin-1)."""
    if not isinstance(err, UnicodeDecodeError):
        raise err
    return "".join(_LEGACY[b] for b in err.object[err.start : err.end]), err.end


codecs.register_error("docingest-legacy", _legacy_bytes)


def decode_tex(data: bytes) -> str:
    """UTF-8, cp1252 or latin-1, decided per byte where needed (never fails); normalized
    newlines.

    A UTF-8 file with one pasted latin-1 byte (a no-break space) must not have every
    accented letter garbled, so only the bytes that are not UTF-8 are read as cp1252.
    A file without a single multi-byte UTF-8 sequence is a legacy file: cp1252, or
    latin-1 if it uses a byte that cp1252 leaves undefined.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if not data.decode("utf-8", errors="ignore").isascii():  # some real UTF-8
            text = data.decode("utf-8", errors="docingest-legacy")
        else:
            try:
                text = data.decode("cp1252")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Inline commands whose argument LaTeX reads verbatim, like \verb|...|: a % or an
# \endinput in there is text. \verb's delimiter can be any character, even a brace;
# the others also accept a balanced {...}. TikZ's \path[...] (...) is not url's \path.
_INLINE_VERB = (
    r"\\(?:(?P<verb>verb)\*?(?=[^a-zA-Z\s*])"
    r"|(?:lstinline|Verb\*?)(?![a-zA-Z@])[ \t]*(?:\[[^\]\n]*\][ \t]*)?(?=[^a-zA-Z\s])"
    r"|mintinline(?![a-zA-Z@])[ \t]*(?:\[[^\]\n]*\][ \t]*)?\{[^{}\n]*\}[ \t]*(?=[^a-zA-Z\s])"
    r"|path(?![a-zA-Z@])[ \t]*(?=[^a-zA-Z\s(\[]))"
)
_INLINE_VERB_MAX = 1000  # chars; bounds the scan, so a line of stray heads stays linear
_VERB_GROUP = re.compile(r"\{(?:[^{}\n]|\{(?:[^{}\n]|\{[^{}\n]*\})*\})*\}")  # 3 levels deep


def _inline_verb_end(text: str, m: re.Match[str]) -> int | None:
    """Index just past the verbatim argument whose command ``m`` matched (an
    ``_INLINE_VERB`` head); None when it does not close on its line, as TeX requires."""
    i = m.end()
    stop = min(len(text), i + _INLINE_VERB_MAX)
    if (nl := text.find("\n", i, stop)) >= 0:
        stop = nl
    if text[i] == "{" and m.group("verb") is None:
        group = _VERB_GROUP.match(text, i, stop)
        return group.end() if group else None
    close = text.find(text[i], i + 1, stop)
    return close + 1 if close >= 0 else None


_SCAN = re.compile(
    rf"(?P<inline>{_INLINE_VERB})"  # \verb|...|, \lstinline!...!, ...
    rf"|\\begin\s*\{{(?P<env>{_VERBATIM_ENVS})\}}"
    r"|\\(?:url|href)\s*\{[^{}\n]*\}"  # a % inside a URL is not a comment
    r"|\\."  # escaped char: \%, \\, \{ ...
    r"|%"
)


def _end_env(env: str) -> re.Pattern[str]:
    return re.compile(rf"\\end\s*\{{{re.escape(env)}\}}")


@dataclass(frozen=True)
class Verbatim:
    start: int
    end: int
    body: str  # what LaTeX reads verbatim; an environment's [options]{args} included
    env: str | None  # None for a \verb-like inline command


# filecontents is written out verbatim, never typeset: nothing in it is a command either.
_VERBATIM_START = re.compile(
    rf"\\begin\s*\{{(?P<env>{_VERBATIM_ENVS}|filecontents\*?)\}}|(?P<inline>{_INLINE_VERB})"
)


def verbatim_parts(text: str, start: int = 0) -> Iterator[Verbatim]:
    """The verbatim regions of ``text[start:]``, in order: verbatim-like environments
    (an unclosed one runs to the end, as in TeX) and ``\\verb``-like arguments.

    Linear: the regex ``\\begin{verbatim}.*?\\end{verbatim}`` rescans the rest of the
    text for every unclosed begin, which takes minutes on a few hundred KB of them.
    """
    pos = start
    while m := _VERBATIM_START.search(text, pos):
        if env := m.group("env"):
            end = _end_env(env).search(text, m.end())
            if end is None:
                yield Verbatim(m.start(), len(text), text[m.end() :], env)
                return
            yield Verbatim(m.start(), end.end(), text[m.end() : end.start()], env)
            pos = end.end()
        elif (end := _inline_verb_end(text, m)) is not None:
            yield Verbatim(m.start(), end, text[m.end() + 1 : end - 1], None)
            pos = end
        else:
            pos = m.end()


def _outside_verbatim(text: str, start: int = 0) -> Iterator[tuple[int, int]]:
    """``(start, end)`` of the stretches between the ``verbatim_parts``, in order."""
    pos = start
    for v in verbatim_parts(text, start):
        yield pos, v.start
        pos = v.end
    yield pos, len(text)


def regions(
    text: str,
    begin: re.Pattern[str],
    end: re.Pattern[str] | Callable[[re.Match[str]], re.Pattern[str]],
) -> Iterator[tuple[re.Match[str], re.Match[str]]]:
    """Non-overlapping ``(begin, end)`` match pairs, the ones ``begin.*?end`` (DOTALL)
    would find, in linear time: that regex rescans the rest of the text for every
    begin without an end. ``end`` may depend on the begin match (an environment's
    name); an end that is missing after one begin is missing after every later one.
    """
    pos = 0
    unclosed: set[str] = set()
    for b in begin.finditer(text):
        if b.start() < pos:
            continue
        stop = end(b) if callable(end) else end
        if stop.pattern in unclosed:
            continue
        if (e := stop.search(text, b.end())) is None:
            unclosed.add(stop.pattern)
            continue
        yield b, e
        pos = e.end()


def sub_regions(
    text: str,
    begin: re.Pattern[str],
    end: re.Pattern[str] | Callable[[re.Match[str]], re.Pattern[str]],
    repl: Callable[[str], str],
) -> str:
    """``text`` with every ``regions`` pair, and what is between, replaced by
    ``repl(what is between)``."""
    parts: list[str] = []
    pos = 0
    for b, e in regions(text, begin, end):
        parts += (text[pos : b.start()], repl(text[b.end() : e.start()]))
        pos = e.end()
    parts.append(text[pos:])
    return "".join(parts)


def strip_comments(text: str) -> str:
    """Drop ``%`` comments like TeX does, except inside verbatim-like environments.

    A comment-only line disappears entirely; a trailing comment keeps its ``%`` so the
    "no space at end of line" meaning survives. ``\\%`` is text, and so is a ``%`` in
    ``\\verb|%|``, ``\\lstinline!%!``, ``\\Verb``, ``\\mintinline`` or ``\\path``.
    """
    out: list[str] = []
    env: str | None = None
    for line in text.splitlines(keepends=True):
        if env is not None:
            out.append(line)
            if _end_env(env).search(line):
                env = None
            continue
        kept, env = _strip_line(line)
        if kept is not None:
            out.append(kept)
    return "".join(out)


def _strip_line(line: str) -> tuple[str | None, str | None]:
    """(line without its comment, or None to drop it; verbatim env left open)."""
    pos = 0
    while m := _SCAN.search(line, pos):
        if m.group("inline") is not None:
            end = _inline_verb_end(line, m)
            if end is None:
                return line, None
            pos = end
        elif env := m.group("env"):
            end = _end_env(env).search(line, m.end())
            if end is None:
                return line, env
            pos = end.end()
        elif m.group() == "%":
            head = line[: m.start()]
            if not head.strip():
                return None, None
            return head + "%" + ("\n" if line.endswith("\n") else ""), None
        else:
            pos = m.end()
    return line, None


def document_body(text: str) -> str:
    """What is between ``\\begin{document}`` and ``\\end{document}`` (all if absent)."""
    begin = re.search(r"\\begin\s*\{document\}", text)
    if begin is None:
        return text
    ends = list(re.finditer(r"\\end\s*\{document\}", text))
    end = ends[-1].start() if ends and ends[-1].start() > begin.end() else len(text)
    return text[begin.end() : end]


def match_brace(text: str, i: int, limit: int = MAX_MACRO_BODY) -> int | None:
    """Index just past the ``}`` closing the ``{`` at ``text[i]``; None if unbalanced."""
    if i >= len(text) or text[i] != "{":
        return None
    depth, j, stop = 0, i, min(len(text), i + limit)
    while j < stop:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


def brace_pairs(text: str) -> dict[int, int]:
    """Map each ``{`` index to the index just past its matching ``}``, in one linear pass.

    Same matching as :func:`match_brace` (``\\`` escapes the next char), but computed once
    per text: scanning a window per candidate went quadratic on unbalanced sources.
    """
    pairs: dict[int, int] = {}
    stack: list[int] = []
    j, n = 0, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            stack.append(j)
        elif c == "}" and stack:
            pairs[stack.pop()] = j + 1
        j += 1
    return pairs


def _matched(pairs: dict[int, int], i: int, limit: int = MAX_MACRO_BODY) -> int | None:
    end = pairs.get(i)
    return end if end is not None and end - i <= limit else None


# ------------------------------------------------------------ macro hygiene
_DEF_HEAD = re.compile(
    r"\\(?P<kind>(?:re)?newcommand|providecommand|DeclareRobustCommand|DeclareMathOperator)"
    r"\*?\s*(?:\{\s*\\(?P<n1>[a-zA-Z@]+)\s*\}|\\(?P<n2>[a-zA-Z@]+))"
    r"(?:\s*\[[^\]\n]*\]){0,2}\s*(?=\{)"
    r"|\\(?P<dkind>[gex]?def)\s*\\(?P<n3>[a-zA-Z@]+)(?:\s*#\d)*\s*(?=\{)"
)
_NEWTHEOREM = re.compile(
    r"\\newtheorem\*?\s*\{[^{}]*\}\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}\s*(?:\[[^\]]*\])?"
)


@dataclass(frozen=True)
class MacroDef:
    start: int
    end: int
    kind: str  # newcommand, renewcommand, def, DeclareMathOperator, ...
    name: str  # without the backslash
    body: str

    @property
    def recursive(self) -> bool:
        return re.search(rf"\\{re.escape(self.name)}{_NOT_LETTER}", self.body) is not None


def macro_definitions(text: str) -> Iterator[MacroDef]:
    """Top-level ``\\newcommand``-style and ``\\def`` definitions with a braced body."""
    pos = 0
    pairs = brace_pairs(text)
    while m := _DEF_HEAD.search(text, pos):
        end = _matched(pairs, m.end())
        if end is None:
            pos = m.end()
            continue
        name = m.group("n1") or m.group("n2") or m.group("n3")
        kind = m.group("kind") or m.group("dkind")
        yield MacroDef(m.start(), end, kind, name, text[m.end() + 1 : end - 1])
        pos = end


def drop_unsafe_macros(text: str) -> tuple[str, list[str]]:
    """Remove definitions pandoc would loop on (self-referential) or that hide structure.

    TeX never expands ``\\small`` inside ``\\@setfontsize\\small``; pandoc does, forever.
    """
    parts: list[str] = []
    dropped: list[str] = []
    pos = 0
    for d in macro_definitions(text):
        if d.recursive or d.name in STRUCTURAL:
            parts.append(text[pos : d.start])
            dropped.append(d.name)
            pos = d.end
    parts.append(text[pos:])
    return "".join(parts), dropped


def package_macros(sty: str) -> str:
    """The notation macros of a local ``.sty``: what pandoc needs, nothing it chokes on.

    Layout machinery (``@`` internals, ``\\renewcommand`` of LaTeX commands) is skipped:
    pandoc cannot typeset anyway, and those definitions are the ones that hang it.
    """
    keep = [
        sty[d.start : d.end]
        for d in macro_definitions(sty)
        if d.kind != "renewcommand"
        and "@" not in d.name
        and "@" not in sty[d.start : d.end]
        and d.name not in STRUCTURAL
        and not d.recursive
    ]
    keep += [m.group() for m in _NEWTHEOREM.finditer(sty)]
    return "\n".join(keep)


_THEBIB_BEGIN = re.compile(r"\\begin\s*\{thebibliography\}\s*(?:\{[^{}]*\})?")
_THEBIB_END = re.compile(r"\\end\s*\{thebibliography\}")
_BIBITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{(?P<key>[^{}]*)\}")


def rewrite_bibliography(text: str) -> str:
    """``thebibliography`` -> a "References" section with one list item per entry.

    pandoc prints the widest-label argument as text, drops ``\\newblock {\\em ...}``
    groups (the venue) and emits no heading; a plain enumerate avoids all three. Each
    item starts with its key as a citation, so ``[@key]`` in the text can be matched.
    """

    def _rewrite(body: str) -> str:
        body = body.replace("\\newblock", " ")
        first = _BIBITEM.search(body)
        if first is None:
            return ""
        # natbib's preamble (\providecommand{\url}..., \expandafter\ifx\csname urlstyle...)
        # only styles the list; keep its helper macros, not \url/\doi overrides or junk.
        preamble = body[: first.start()]
        head = "\n".join(
            preamble[d.start : d.end]
            for d in macro_definitions(preamble)
            if d.name not in ("url", "doi", "href", "urlprefix")
        )
        items = _BIBITEM.sub(
            lambda b: f"\\item \\cite{{{b.group('key').strip()}}} ", body[first.start() :]
        )
        return (
            f"{head}\n\\section*{{References}}\n\\begin{{enumerate}}\n{items}\n\\end{{enumerate}}\n"
        )

    return sub_regions(text, _THEBIB_BEGIN, _THEBIB_END, _rewrite)


def prepare_for_pandoc(text: str) -> tuple[str, list[str]]:
    """Source rewrites that make pandoc's output complete and its run bounded."""
    text, dropped = drop_unsafe_macros(text)
    text = rewrite_bibliography(text)
    # NeurIPS/ICLR styles separate authors with \And / \AND: pandoc only knows \and.
    text = re.sub(rf"\\(?:And|AND){_NOT_LETTER}", r"\\and", text)
    text = re.sub(rf"\\today{_NOT_LETTER}", "", text)  # sandboxed pandoc prints 1970-01-01
    warnings = []
    if dropped:
        names = ", ".join(sorted({f"\\{n}" for n in dropped}))
        warnings.append(f"ignored {len(dropped)} self-referential/structural macro(s): {names}")
    return text, warnings


# ------------------------------------------------------------------ flatten
@dataclass
class Flattened:
    text: str
    files: list[str] = field(default_factory=list)  # inlined files, relative to the root
    warnings: list[str] = field(default_factory=list)


# Applied outside verbatim regions only (see _Flattener._expand).
_FLATTEN = re.compile(
    rf"\\(?P<input>input|include|subfile|expandableinput){_NOT_LETTER}\s*"
    r"(?:\{(?P<arg>[^{}]*)\}|(?P<bare>[^\s{}\\%]+))"
    r"|\\(?P<imp>(?:sub)?(?:import|inputfrom|includefrom))\*?\s*"
    r"\{(?P<dir>[^{}]*)\}\s*\{(?P<file>[^{}]*)\}"
    rf"|\\(?:usepackage|RequirePackage){_NOT_LETTER}\s*(?P<opts>\[[^\]]*\])?\s*"
    r"\{(?P<pkgs>[^{}]*)\}"
    rf"|(?P<bib>\\bibliography{_NOT_LETTER}\s*\{{[^{{}}]*\}})"
)
_ENDINPUT = re.compile(rf"\\endinput{_NOT_LETTER}")
_ENDINPUT_TOKEN = re.compile(rf"(?P<endinput>\\endinput{_NOT_LETTER})|\\.|(?P<brace>[{{}}])")
_END_DOCUMENT = re.compile(r"\\end\s*\{document\}")
# An \endinput TeX does not execute on a first read: an include guard closed on the same
# line (\ifx\loaded\undefined\else\endinput\fi) or the operand of \let, \ifx, ...
_GUARDED = re.compile(rf"[^\n]{{0,200}}?\\fi{_NOT_LETTER}")
_OPERAND = re.compile(
    r"\\(?:(?:future)?let\s*\\[a-zA-Z@]+\s*=?|ifx(?:\s*\\[a-zA-Z@]+)?|noexpand|string|meaning|show)"
    r"\s*\Z"
)


def _executed(text: str, m: re.Match[str]) -> bool:
    return not (
        _GUARDED.match(text, m.end()) or _OPERAND.search(text, max(0, m.start() - 80), m.start())
    )


def cut_at_endinput(text: str, *, main: bool = False) -> str:
    """``text`` as far as TeX reads it: to the end of the line holding the first
    ``\\endinput`` TeX executes (the rest of that line is still read). The command
    itself is dropped: pandoc would stop reading the whole flattened document there.

    Only an ``\\endinput`` at brace depth 0 counts, outside verbatim and filecontents
    environments and ``\\verb``-like arguments: one in a macro body, as a ``\\let``
    operand or in an include guard is not executed. LaTeX has to reach
    ``\\end{document}`` in the main file, so there only one after it counts.
    """
    start = 0
    if main and (ends := [m.end() for m in _END_DOCUMENT.finditer(text)]):
        start = ends[-1]
    if not _ENDINPUT.search(text, start):
        return text
    depth = 0
    for a, b in _outside_verbatim(text, start):
        for m in _ENDINPUT_TOKEN.finditer(text, a, b):
            if brace := m.group("brace"):
                depth = depth + 1 if brace == "{" else max(depth - 1, 0)
            elif m.group("endinput") and depth == 0 and _executed(text, m):
                eol = text.find("\n", m.end())
                eol = len(text) if eol < 0 else eol + 1
                return text[: m.start()] + text[m.end() : eol]
    return text


class _Flattener:
    def __init__(self, main: Path, boundary: Path, max_chars: int, max_depth: int):
        self.main = main.resolve()
        self.boundary = boundary.resolve()
        self.max_chars = max_chars
        self.max_depth = max_depth
        self.size = 0
        self.files: list[str] = []
        self.warnings: list[str] = []
        self.packages: set[Path] = set()
        bbl = self.main.with_suffix(".bbl")  # what \bibliography reads: <jobname>.bbl
        self.bbl: Path | None = bbl if bbl.is_file() else None

    def run(self) -> Flattened:
        text = self._load(self.main)
        text = self._expand(text, self.main, self.main.parent, (self.main,))
        return Flattened(text, self.files, self.warnings)

    # -- files
    def _rel(self, path: Path) -> str:
        return path.relative_to(self.boundary).as_posix()

    def _load(self, path: Path, *, endinput: bool = True) -> str:
        size = path.stat().st_size
        self.size += size
        if self.size > self.max_chars:
            raise ConversionError(f"LaTeX source exceeds {_size(self.max_chars)} (max_archive_mb)")
        text = strip_comments(decode_tex(path.read_bytes()))
        return cut_at_endinput(text, main=path == self.main) if endinput else text

    def _resolve(self, name: str, dirs: list[Path], suffix: str, always: bool) -> Path | str:
        """The file for ``name``, or why there is none."""
        name = name.strip().strip('"').strip()
        if not name or "#" in name or "\\" in name:
            return "unresolvable"
        if name.endswith(suffix):
            names = [name]
        else:
            names = [name + suffix] if always else [name + suffix, name]
        outside = False
        for d in dict.fromkeys(dirs):
            for n in names:
                try:
                    p = (d / n).resolve()
                except (OSError, RuntimeError):
                    continue
                if not p.is_relative_to(self.boundary):
                    outside = True
                elif p.is_file():
                    return p
        return "outside the source tree" if outside else "missing"

    # -- expansion
    def _expand(self, text: str, current: Path, base: Path, stack: tuple[Path, ...]) -> str:
        """Inline the includes of ``text``; verbatim regions are copied untouched."""
        parts: list[str] = []
        pos = 0
        for a, b in _outside_verbatim(text):
            chunk = _FLATTEN.sub(lambda m: self._replace(m, current, base, stack), text[a:b])
            parts += (text[pos:a], chunk)
            pos = b
        return "".join(parts)

    def _replace(self, m: re.Match[str], current: Path, base: Path, stack: tuple[Path, ...]) -> str:
        nl = m.string[m.end() : m.end() + 1] == "\n"
        if cmd := m.group("input"):
            name = m.group("arg") if m.group("arg") is not None else m.group("bare")
            dirs = [base, current.parent, self.main.parent]
            path = self._resolve(name, dirs, ".tex", always=cmd == "include")
            return self._inline(m.group(), path, base, stack, nl=nl, page_break=cmd == "include")
        if imp := m.group("imp"):
            anchor = base if imp.startswith("sub") else self.main.parent
            d = anchor / m.group("dir").strip()
            path = self._resolve(m.group("file"), [d], ".tex", always=False)
            return self._inline(m.group(), path, d, stack, nl=nl, page_break="include" in imp)
        if m.group("pkgs") is not None:
            return self._packages(m, current, base, stack)
        return self._bibliography(m.group())

    def _skip(self, command: str, why: str, nl: bool) -> str:
        self.warnings.append(f"skipped {command} ({why})")
        return f"%docingest: skipped ({why})" + ("" if nl else "\n")

    def _inline(
        self,
        command: str,
        path: Path | str,
        base: Path,
        stack: tuple[Path, ...],
        *,
        nl: bool,
        page_break: bool,
    ) -> str:
        if isinstance(path, str):
            return self._skip(command, path, nl)
        if path in stack:
            return self._skip(command, "recursive include", nl)
        if len(stack) > self.max_depth:
            return self._skip(command, f"nesting deeper than {self.max_depth}", nl)
        text = self._load(path)
        if re.search(r"\\begin\s*\{document\}", text):  # \subfile, standalone figures
            text = document_body(text)
        self.files.append(self._rel(path))
        text = self._expand(text, path, base, (*stack, path)).rstrip("\n")
        if page_break:  # \include always starts a new page, hence a new paragraph
            return f"\n{text}\n\n"
        return text + ("" if nl else "\n")

    def _packages(
        self, m: re.Match[str], current: Path, base: Path, stack: tuple[Path, ...]
    ) -> str:
        names = [n.strip() for n in m.group("pkgs").split(",") if n.strip()]
        dirs = [base, current.parent, self.main.parent]
        system, macros = [], []
        for name in names:
            path = self._resolve(name, dirs, ".sty", always=True)
            if isinstance(path, str):
                system.append(name)  # amsmath & co.: pandoc knows them or ignores them
            elif path not in self.packages and path not in stack:  # LaTeX loads a package once
                self.packages.add(path)
                self.files.append(self._rel(path))
                sty = self._expand(self._load(path, endinput=False), path, base, (*stack, path))
                macros.append(package_macros(sty))
        if len(system) == len(names):
            return m.group()
        keep = f"\\usepackage{m.group('opts') or ''}{{{','.join(system)}}}" if system else ""
        return "\n".join(x for x in (keep, *macros) if x) + "\n"

    def _bibliography(self, command: str) -> str:
        if self.bbl is None:
            return command
        bbl, self.bbl = self._load(self.bbl), None  # inline once
        if not re.search(r"\\begin\s*\{thebibliography\}", bbl):  # biblatex: leave it
            return command
        self.files.append(self.main.with_suffix(".bbl").relative_to(self.boundary).as_posix())
        return f"\n{bbl.strip()}\n"


def flatten(main: Path, boundary: Path, *, max_chars: int, max_depth: int = MAX_DEPTH) -> Flattened:
    """Inline ``\\input``/``\\include``/``\\subfile``/``\\import``, local ``.sty`` macros
    and the ``<main>.bbl`` bibliography, recursively, into one string.

    Includes resolve like TeX (relative to the main file's directory) with the including
    file's directory as a fallback; nothing outside ``boundary`` is ever read. Missing,
    recursive or too-deep includes become a ``%docingest:`` comment plus a warning.
    Raises ``ConversionError`` when the total exceeds ``max_chars``.
    """
    return _Flattener(main, boundary, max_chars, max_depth).run()


# ---------------------------------------------------------------- main file
_DOCCLASS = re.compile(r"^[^%\n]*\\document(?:class|style)\s*(?:\[[^\]]*\])?\s*\{?([\w-]*)", re.M)
_BEGIN_DOC = re.compile(r"\\begin\s*\{document\}")
_INCLUDED = re.compile(
    rf"\\(?:input|include|subfile|(?:sub)?import\s*\{{[^{{}}]*\}}){_NOT_LETTER}\s*\{{([^{{}}]+)\}}"
)


def _readme_main(root: Path) -> Path | None:
    """arXiv's 00README.json (``usage: toplevel``) or legacy ``00README.XXX``."""
    readme = root / "00README.json"
    if readme.is_file():
        try:
            data = json.loads(decode_tex(readme.read_bytes()))
        except ValueError:
            data = {}
        sources = data.get("sources", []) if isinstance(data, dict) else []
        for src in sources if isinstance(sources, list) else []:
            if (
                isinstance(src, dict)
                and src.get("usage") == "toplevel"
                and (p := _child(root, str(src.get("filename", ""))))
            ):
                return p
    for legacy in sorted(root.glob("00README*")):
        if legacy.suffix.lower() == ".json" or not legacy.is_file():
            continue
        for line in decode_tex(legacy.read_bytes()).splitlines():
            parts = line.split()
            if (
                len(parts) >= 2
                and parts[1].lower() == "toplevelfile"
                and (p := _child(root, parts[0]) or _child(root, parts[0] + ".tex"))
            ):
                return p
    return None


def _child(root: Path, name: str) -> Path | None:
    if not name:
        return None
    p = (root / name).resolve()
    return p if p.is_relative_to(root.resolve()) and p.is_file() else None


def find_main(root: Path) -> tuple[Path, list[str]]:
    """The file to compile: README hint, else ``\\documentclass`` + ``\\begin{document}``,
    else the conventional name, else the file nobody includes, else the largest."""
    if (hinted := _readme_main(root)) is not None:
        return hinted, []
    files = [
        p
        for p in sorted(root.rglob("*"))
        if p.suffix.lower() in TEX_SUFFIXES
        and p.is_file()
        and not any(part.startswith((".", "__MACOSX")) for part in p.relative_to(root).parts)
    ]
    if not files:
        raise ConversionError("no .tex file in the LaTeX source")
    texts = {p: strip_comments(decode_tex(p.read_bytes())) for p in files}

    def doc_class(p: Path) -> str | None:
        m = _DOCCLASS.search(texts[p])
        return m.group(1) if m else None

    full = [p for p in files if doc_class(p) is not None and _BEGIN_DOC.search(texts[p])]
    real = [p for p in full if doc_class(p) not in ("standalone", "subfiles")]
    pool = real or full or [p for p in files if doc_class(p) is not None] or files
    if len(pool) == 1:
        return pool[0], []

    named = [p for p in pool if p.stem.lower() in MAIN_NAMES]
    if named:
        best = min(
            named, key=lambda p: (len(p.relative_to(root).parts), MAIN_NAMES.index(p.stem.lower()))
        )
        return best, []
    included = set()
    for p, text in texts.items():
        for m in _INCLUDED.finditer(text):
            target = m.group(1).strip()
            for d in (root, p.parent):
                included.add((d / target).resolve())
                included.add((d / (target + ".tex")).resolve())
    top = [p for p in pool if p.resolve() not in included] or pool
    best = max(top, key=lambda p: p.stat().st_size)
    if len(top) > 1:
        others = ", ".join(p.relative_to(root).as_posix() for p in top if p != best)
        return best, [
            f"several main-file candidates; picked the largest ({best.name}) over {others}"
        ]
    return best, []


# ------------------------------------------------------------------- unpack
@dataclass(frozen=True)
class Unpacked:
    root: Path  # nothing outside it is read
    main: Path | None  # None: an archive, call find_main(root)
    warnings: list[str] = field(default_factory=list)


def _is_tar(block: bytes) -> bool:
    """A valid first tar header: ustar or pre-POSIX (v7) without the magic.

    Checks the header checksum on this one block, like the detector does, so every
    archive the detector routes here is unpacked as an archive.
    """
    try:
        tarfile.TarInfo.frombuf(block[: tarfile.BLOCKSIZE], tarfile.ENCODING, "surrogateescape")
    except tarfile.HeaderError:
        return False
    return True


def unpack(path: Path, dest: Path, *, max_bytes: int) -> Unpacked:
    """Plain ``.tex`` -> itself; tar / tar.gz -> extracted into ``dest``; a single
    gzipped ``.tex`` (old arXiv e-prints) -> ``dest/main.tex``.

    Archives are capped at ``max_bytes`` unpacked (checked while reading headers, so a
    zip bomb is refused early) and extracted with tarfile's ``data`` filter.
    """
    with path.open("rb") as f:
        head = f.read(512)
    if head.startswith(_GZIP_MAGIC):
        try:
            with gzip.open(path) as g:
                block = g.read(512)
        except (OSError, EOFError, zlib.error) as e:
            raise ConversionError(f"corrupt gzip file {path.name}: {e}") from e
        if _is_tar(block):
            return _untar(path, dest, max_bytes, gzipped=True)
        dest.mkdir(parents=True, exist_ok=True)
        main = dest / "main.tex"
        _gunzip(path, main, max_bytes)
        return Unpacked(dest, main)
    if _is_tar(head):
        return _untar(path, dest, max_bytes, gzipped=False)
    return Unpacked(path.parent, path)


def _size(n: int) -> str:
    return f"{n / 2**20:.0f} MB" if n >= 2**20 else f"{n} bytes"


def _too_big(name: str, max_bytes: int) -> ConversionError:
    return ConversionError(f"{name} unpacks to more than {_size(max_bytes)} (max_archive_mb)")


def _gunzip(src: Path, dst: Path, max_bytes: int) -> None:
    written = 0
    try:
        with gzip.open(src) as g, dst.open("wb") as out:
            while chunk := g.read(1 << 20):
                written += len(chunk)
                if written > max_bytes:
                    raise _too_big(src.name, max_bytes)
                out.write(chunk)
    except (OSError, EOFError, zlib.error) as e:
        raise ConversionError(f"corrupt gzip file {src.name}: {e}") from e


class _CappedReader:
    """The archive stream tarfile reads through, handing out at most ``room`` more bytes.

    tarfile reads a pax or GNU long-name header into memory whole, inside ``next()``,
    before the loop in ``_untar`` sees the member it belongs to: 150 KB of gzip make a
    150 MB "header" and several times that in memory, so ``max_archive_mb`` alone bounds
    nothing. A read that would exceed the room is refused before it decompresses or
    allocates anything. Seeking (skipping member data) is not reading: the loop caps it.
    """

    def __init__(self, raw: io.BufferedIOBase, label: str):
        self.raw = raw
        self.label = label
        self.room = MAX_TAR_HEADER

    def read(self, size: int = -1, /) -> bytes:
        if size < 0 or size > self.room:
            what = _size(size) if size >= 0 else "the rest"
            raise ConversionError(
                f"cannot unpack {self.label}: refusing to read {what} of tar headers "
                f"at once (limit {_size(MAX_TAR_HEADER)}; a pax or GNU long-name bomb?)"
            )
        data = self.raw.read(size)
        self.room -= len(data)
        return data

    def seek(self, pos: int, whence: int = io.SEEK_SET, /) -> int:
        return self.raw.seek(pos, whence)

    def tell(self) -> int:
        return self.raw.tell()

    def write(self, b: bytes, /) -> int:
        raise io.UnsupportedOperation("read-only archive stream")

    def close(self) -> None:
        self.raw.close()


def _untar(path: Path, dest: Path, max_bytes: int, *, gzipped: bool) -> Unpacked:
    dest.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    wanted: list[tarfile.TarInfo] = []
    total = 0
    try:
        with gzip.open(path) if gzipped else path.open("rb") as raw:
            reader = _CappedReader(raw, path.name)
            with tarfile.open(fileobj=reader, mode="r:") as tar:
                for i, member in enumerate(tar):  # headers are read lazily: abort early
                    reader.room = MAX_TAR_HEADER  # for the headers of the next member
                    if i >= MAX_MEMBERS:
                        raise ConversionError(f"{path.name} has more than {MAX_MEMBERS} files")
                    total += max(member.size, 0)
                    if total > max_bytes:
                        raise _too_big(path.name, max_bytes)
                    if not member.isfile() or Path(member.name).suffix.lower() in _MEDIA_SUFFIXES:
                        continue  # links, devices, figures: never needed for text
                    try:
                        tarfile.data_filter(member, str(dest))
                    except tarfile.FilterError as e:
                        warnings.append(f"skipped unsafe archive member {member.name!r}: {e}")
                        continue
                    wanted.append(member)
                reader.room = sum(m.size for m in wanted)  # extracting reads exactly that
                tar.extractall(dest, members=wanted, filter="data")
    except MemoryError as e:  # a last resort: any allocation above must not kill the run
        raise ConversionError(f"cannot unpack {path.name}: out of memory") from e
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise ConversionError(f"cannot unpack {path.name}: {e}") from e
    return Unpacked(dest, None, warnings)


# ----------------------------------------------------------------- measures
_UNTEXT_ENVS = r"tikzpicture|pgfpicture|axis|filecontents\*?|comment"
_UNTEXT_BEGIN = re.compile(rf"\\begin\{{({_UNTEXT_ENVS})\}}")
IFFALSE = re.compile(r"\\iffalse\b")
FI = re.compile(r"\\fi\b")


def approx_text_length(latex: str) -> int:
    """Rough count of the readable characters in a document, to sanity-check output."""
    body = document_body(latex)
    body = sub_regions(body, _UNTEXT_BEGIN, lambda b: _end_env(b.group(1)), lambda _: " ")
    body = sub_regions(body, IFFALSE, FI, lambda _: " ")
    body = re.sub(r"\\[a-zA-Z@]+\*?", " ", body)
    body = re.sub(r"[{}\[\]\\$&^_~#%]", " ", body)
    return len(" ".join(body.split()))


_SECTION = re.compile(
    r"\\(?P<cmd>part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*(?=\{)"
)


def split_sections(body: str, max_level: int) -> list[tuple[int, str | None, str]]:
    """Split LaTeX at sectioning commands of level <= ``max_level``.

    Returns ``(level, title LaTeX, content LaTeX)``; content before the first heading
    comes first with level 0 and no title. Levels follow pandoc: ``\\section`` is 1,
    unless the document has chapters, which then take level 1.
    """
    chapters = re.search(r"\\chapter\*?\s*[\[{]", body) is not None
    levels = {"part": 0, "chapter": 1, "section": 1 + chapters}
    levels["subsection"] = levels["section"] + 1
    levels["subsubsection"] = levels["section"] + 2
    cuts: list[tuple[int, int, int, str]] = []
    pairs = brace_pairs(body)
    for m in _SECTION.finditer(body):
        level = levels[m.group("cmd")]
        end = _matched(pairs, m.end())
        if level <= max_level and end is not None:
            cuts.append((m.start(), end, max(level, 1), body[m.end() + 1 : end - 1]))
    starts = [c[0] for c in cuts] + [len(body)]
    parts: list[tuple[int, str | None, str]] = [(0, None, body[: starts[0]])]
    for (_, end, level, title), nxt in zip(cuts, starts[1:], strict=True):
        parts.append((level, title, body[end:nxt]))
    return parts
