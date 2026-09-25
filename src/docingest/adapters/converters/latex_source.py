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

import gzip
import json
import re
import tarfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.errors import ConversionError

TEX_SUFFIXES = frozenset({".tex", ".ltx"})
MAIN_NAMES = ("main", "ms", "paper", "article")  # tie-break between several main files
MAX_DEPTH = 20  # nested \input levels
MAX_MEMBERS = 20_000  # files in one archive
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
def decode_tex(data: bytes) -> str:
    """UTF-8, else cp1252, else latin-1 (never fails); normalized newlines."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for encoding in ("utf-8", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


_SCAN = re.compile(
    r"\\verb\*?(?P<delim>[^a-zA-Z\s*])"  # \verb|...| : any delimiter
    rf"|\\begin\s*\{{(?P<env>{_VERBATIM_ENVS})\}}"
    r"|\\(?:url|href)\s*\{[^{}\n]*\}"  # a % inside a URL is not a comment
    r"|\\."  # escaped char: \%, \\, \{ ...
    r"|%"
)


def _end_env(env: str) -> re.Pattern[str]:
    return re.compile(rf"\\end\s*\{{{re.escape(env)}\}}")


def strip_comments(text: str) -> str:
    """Drop ``%`` comments like TeX does, except inside verbatim-like environments.

    A comment-only line disappears entirely; a trailing comment keeps its ``%`` so the
    "no space at end of line" meaning survives. ``\\%`` and ``\\verb|%|`` are text.
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
        if delim := m.group("delim"):
            close = line.find(delim, m.end())
            if close < 0:
                return line, None
            pos = close + 1
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
    while m := _DEF_HEAD.search(text, pos):
        end = match_brace(text, m.end())
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


_THEBIB = re.compile(
    r"\\begin\s*\{thebibliography\}\s*(?:\{[^{}]*\})?(?P<body>.*?)\\end\s*\{thebibliography\}",
    re.DOTALL,
)
_BIBITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{(?P<key>[^{}]*)\}")


def rewrite_bibliography(text: str) -> str:
    """``thebibliography`` -> a "References" section with one list item per entry.

    pandoc prints the widest-label argument as text, drops ``\\newblock {\\em ...}``
    groups (the venue) and emits no heading; a plain enumerate avoids all three. Each
    item starts with its key as a citation, so ``[@key]`` in the text can be matched.
    """

    def _rewrite(m: re.Match[str]) -> str:
        body = m.group("body").replace("\\newblock", " ")
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

    return _THEBIB.sub(_rewrite, text)


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


_FLATTEN = re.compile(
    rf"(?P<verbatim>\\begin\s*\{{(?P<venv>{_VERBATIM_ENVS})\}}.*?\\end\s*\{{(?P=venv)\}})"
    r"|(?P<verb>\\verb\*?(?P<vd>[^a-zA-Z\s*])[^\n]*?(?P=vd))"
    rf"|\\(?P<input>input|include|subfile|expandableinput){_NOT_LETTER}\s*"
    r"(?:\{(?P<arg>[^{}]*)\}|(?P<bare>[^\s{}\\%]+))"
    r"|\\(?P<imp>(?:sub)?(?:import|inputfrom|includefrom))\*?\s*"
    r"\{(?P<dir>[^{}]*)\}\s*\{(?P<file>[^{}]*)\}"
    rf"|\\(?:usepackage|RequirePackage){_NOT_LETTER}\s*(?P<opts>\[[^\]]*\])?\s*"
    r"\{(?P<pkgs>[^{}]*)\}"
    rf"|(?P<bib>\\bibliography{_NOT_LETTER}\s*\{{[^{{}}]*\}})",
    re.DOTALL,
)
_ENDINPUT = re.compile(rf"\\endinput{_NOT_LETTER}")


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
        if endinput and (m := _ENDINPUT.search(text)):
            text = text[: m.start()]  # TeX stops reading the file there
        return text

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
        return _FLATTEN.sub(lambda m: self._replace(m, current, base, stack), text)

    def _replace(self, m: re.Match[str], current: Path, base: Path, stack: tuple[Path, ...]) -> str:
        if m.group("verbatim") or m.group("verb"):
            return m.group()
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
    return len(block) > 262 and block[257:262] == b"ustar"


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
            return _untar(path, "r:gz", dest, max_bytes)
        dest.mkdir(parents=True, exist_ok=True)
        main = dest / "main.tex"
        _gunzip(path, main, max_bytes)
        return Unpacked(dest, main)
    if _is_tar(head):
        return _untar(path, "r:", dest, max_bytes)
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


def _untar(path: Path, mode: str, dest: Path, max_bytes: int) -> Unpacked:
    dest.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    wanted: list[tarfile.TarInfo] = []
    total = 0
    try:
        with tarfile.open(path, mode) as tar:  # type: ignore[call-overload]
            for i, member in enumerate(tar):  # headers are read lazily: abort early
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
            tar.extractall(dest, members=wanted, filter="data")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise ConversionError(f"cannot unpack {path.name}: {e}") from e
    return Unpacked(dest, None, warnings)


# ----------------------------------------------------------------- measures
_UNTEXT_ENVS = r"tikzpicture|pgfpicture|axis|filecontents\*?|comment"


def approx_text_length(latex: str) -> int:
    """Rough count of the readable characters in a document, to sanity-check output."""
    body = document_body(latex)
    body = re.sub(rf"\\begin\{{({_UNTEXT_ENVS})\}}.*?\\end\{{\1\}}", " ", body, flags=re.S)
    body = re.sub(r"\\iffalse\b.*?\\fi\b", " ", body, flags=re.S)
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
    for m in _SECTION.finditer(body):
        level = levels[m.group("cmd")]
        end = match_brace(body, m.end())
        if level <= max_level and end is not None:
            cuts.append((m.start(), end, max(level, 1), body[m.end() + 1 : end - 1]))
    starts = [c[0] for c in cuts] + [len(body)]
    parts: list[tuple[int, str | None, str]] = [(0, None, body[: starts[0]])]
    for (_, end, level, title), nxt in zip(cuts, starts[1:], strict=True):
        parts.append((level, title, body[end:nxt]))
    return parts
