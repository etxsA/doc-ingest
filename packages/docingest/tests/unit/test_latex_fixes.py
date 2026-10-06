"""Regressions from the third review of the LaTeX adapter (latex_source, pandoc_latex).

Each test failed before its fix; none needs the pandoc binary (the fallback tests point
the converter at a missing one, so pylatexenc does the work).
"""

import gzip
import io
import re
import tarfile
import time
import tracemalloc
from pathlib import Path

import pytest

from docingest.adapters.converters import latex_source
from docingest.adapters.converters.latex_source import (
    MAX_TAR_HEADER,
    approx_text_length,
    cut_at_endinput,
    decode_tex,
    flatten,
    regions,
    rewrite_bibliography,
    strip_comments,
    sub_regions,
    unpack,
    verbatim_parts,
)
from docingest.adapters.converters.pandoc_latex import PandocLatexConverter, split_markdown
from docingest.config import LatexConfig
from docingest.domain.errors import ConversionError
from docingest.domain.models import PageMethod

MB = 2**20
DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---------------------------------------------------------------- \endinput
@pytest.mark.parametrize(
    "text",
    [
        # filecontents writes a package out verbatim: its \endinput is not the main file's
        "\\begin{filecontents*}{notation.sty}\n\\newcommand{\\R}{\\mathbb{R}}\n\\endinput\n"
        "\\end{filecontents*}\nKEEP",
        "End every package file with \\verb|\\endinput| so junk is ignored.\nKEEP",
        "\\lstinline!\\endinput! and \\begin{verbatim}\n\\endinput\n\\end{verbatim}\nKEEP",
        "\\newcommand{\\stopreading}{\\endinput}\nKEEP",  # a macro body, not executed
        "\\let\\stopreading\\endinput\nKEEP",
        "\\ifx\\notationloaded\\undefined\\else\\endinput\\fi\nKEEP",  # include guard
    ],
)
def test_endinput_that_tex_does_not_execute_is_ignored(text):
    assert cut_at_endinput(text) == text


def test_endinput_is_honoured_to_the_end_of_its_line():
    # TeX still reads the rest of the line; the command itself must not reach pandoc,
    # which would stop reading the whole flattened document at it.
    assert cut_at_endinput("kept\n\\endinput rest of line\ndropped") == "kept\n rest of line\n"
    assert cut_at_endinput("a{b}\\endinput") == "a{b}"


def test_main_file_endinput_counts_only_after_end_document():
    body = "Intro \\endinput stray\n\\section{Evaluation}\nLost before the fix."
    main = f"\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"
    assert cut_at_endinput(main, main=True) == main  # LaTeX could not compile otherwise
    trailer = main + "\\endinput\nold draft \\input{notes}\n"
    assert cut_at_endinput(trailer, main=True) == main + "\n"
    assert "Lost before" not in cut_at_endinput(main)  # an included file stops there


def test_flatten_keeps_the_document_after_filecontents_and_verb(tmp_path):
    main = write(
        tmp_path,
        "main.tex",
        "\\begin{filecontents*}{notation.sty}\n\\ProvidesPackage{notation}\n\\endinput\n"
        "\\end{filecontents*}\n" + DOC % "\\section{Intro}\n\\input{part}\n\\section{Results}",
    )
    write(tmp_path, "part.tex", "Write \\verb|\\endinput| last.\nPART-TAIL\n\\endinput\nJUNK")
    text = flatten(main, tmp_path, max_chars=MB).text
    assert "\\section{Results}" in text and "PART-TAIL" in text
    assert "JUNK" not in text and text.count("\\endinput") == 2  # filecontents and \verb


# ------------------------------------------------------------ tar bombs
def _bomb(path: Path, header_type: bytes, size: int) -> Path:
    """A tar.gz whose first member is a ``size``-byte pax / GNU long-name header."""
    info = tarfile.TarInfo("././@LongLink" if header_type == tarfile.GNUTYPE_LONGNAME else "pax")
    info.type, info.size = header_type, size
    body = b"a" * size if header_type == tarfile.GNUTYPE_LONGNAME else b"20 comment=aaaaaaaa\n"
    data = (DOC % "hi").encode()
    main = tarfile.TarInfo("main.tex")
    main.size = len(data)
    with gzip.open(path, "wb") as g:
        g.write(info.tobuf(format=tarfile.USTAR_FORMAT))
        g.write(body.ljust(size, b"\0") + b"\0" * (-size % 512))
        g.write(main.tobuf(format=tarfile.USTAR_FORMAT) + data + b"\0" * (-len(data) % 512))
        g.write(b"\0" * 1024)
    return path


@pytest.mark.parametrize("header_type", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
def test_unpack_refuses_huge_tar_headers_without_reading_them(tmp_path, header_type):
    # ~8 KB of gzip; tarfile used to read the whole header into memory (several times
    # over) inside next(), before max_archive_mb was ever checked.
    bomb = _bomb(tmp_path / "bomb.tar.gz", header_type, 8 * MB)
    assert bomb.stat().st_size < 64 * 1024
    tracemalloc.start()
    try:
        with pytest.raises(ConversionError, match="GNU long-name bomb"):
            unpack(bomb, tmp_path / "work", max_bytes=MB)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 3 * MB


def test_unpack_still_reads_ordinary_pax_headers(tmp_path):
    name = "sections/" + "a-very-long-directory-name/" * 5 + "intro.tex"  # > 100 chars: pax
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for member, data in ((name, b"INTRO"), ("main.tex", b"\\input{x}")):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.pax_headers = {"comment": "x" * 4000}
            tar.addfile(info, io.BytesIO(data))
    archive = tmp_path / "2401.00001"
    archive.write_bytes(buf.getvalue())
    out = unpack(archive, tmp_path / "work", max_bytes=MB)
    assert (out.root / name).read_text() == "INTRO" and out.warnings == []
    assert MAX_TAR_HEADER >= 64 * 1024  # real headers fit comfortably


def test_unpack_turns_memory_errors_into_conversion_errors(tmp_path, monkeypatch):
    archive = tmp_path / "x.tar"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("main.tex"), io.BytesIO(b""))
    archive.write_bytes(buf.getvalue())

    def exhausted(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(latex_source.tarfile, "open", exhausted)
    with pytest.raises(ConversionError, match="out of memory"):
        unpack(archive, tmp_path / "work", max_bytes=MB)


# ------------------------------------------------------- split_markdown
@pytest.mark.parametrize(
    "line",
    [
        "We use a learning rate of $3$$\\times 10^{-4}$.",  # pandoc's $3$ $\times..$
        "Accuracy is $91.2$$\\pm$$0.3$ on the test set, i.e. $x$$^2$.",
    ],
)
def test_split_markdown_is_not_fooled_by_adjacent_inline_math(line):
    md = f"\n# Intro\n\n{line}\n\n# Method\n\nMethod text.\n\n# Results\n\nResults text.\n"
    parts = split_markdown(md, 2)
    assert [title for _, title, _, _ in parts] == [None, "Intro", "Method", "Results"]
    assert "# Method" not in parts[1][3]


# ---------------------------------------------------- inline verbatim
@pytest.mark.parametrize(
    "line",
    [
        "We test parity with \\lstinline!n % 2 == 0! in the loop.",
        "Braced \\lstinline{n % 2 == {0}} and optioned \\lstinline[language=C]|a%b| too.",
        "Minted \\mintinline{python}{x % y} or \\mintinline[style=x]{c}|a%b|.",
        "Fancy \\Verb|50%| and \\Verb*!a % b! and a path \\path|C:\\50%\\x|.",
    ],
)
def test_strip_comments_keeps_percent_inside_inline_verbatim(line):
    assert strip_comments(line + " % a real comment\n") == line + " %\n"


def test_strip_comments_still_strips_after_tikz_path():
    src = "\\path[draw] (0,0) -- (1,1); % comment\n\\path (a) edge (b); % gone\n"
    assert strip_comments(src) == "\\path[draw] (0,0) -- (1,1); %\n\\path (a) edge (b); %\n"


def test_verbatim_parts():
    text = "a \\verb|x%| b \\begin{lstlisting}[language=C]\ny\n\\end{lstlisting} \\lstinline{z}"
    parts = list(verbatim_parts(text))
    assert [(p.env, p.body) for p in parts] == [
        (None, "x%"),
        ("lstlisting", "[language=C]\ny\n"),
        (None, "z"),
    ]
    assert text[parts[1].start : parts[1].end].startswith("\\begin{lstlisting}")
    unclosed = list(verbatim_parts("\\begin{verbatim}\nrest \\input{x}"))
    assert [(p.env, p.body) for p in unclosed] == [("verbatim", "\nrest \\input{x}")]


# ------------------------------------------------- linear-time scanning
N_UNCLOSED = 16_000  # ~280 KB: the lazy-regex versions took 10+ s here, quadratically more


def _seconds(fn, arg) -> float:
    best = float("inf")
    for _ in range(2):  # best of 2: robust to a busy machine
        t0 = time.perf_counter()
        fn(arg)
        best = min(best, time.perf_counter() - t0)
    return best


def _linear(fn, make) -> None:
    """Quadrupling the input must cost far less than 16x (quadratic); load-independent."""
    small, big = _seconds(fn, make(N_UNCLOSED // 4)), _seconds(fn, make(N_UNCLOSED))
    assert big < 10  # backstop: the old regexes took 10+ s at this size
    assert big < max(8 * small, 0.05), (small, big)


def test_unclosed_verbatim_flattens_in_linear_time(tmp_path):
    def make(n):
        return write(tmp_path, f"main{n}.tex", DOC % ("\\begin{verbatim}x\n" * n))

    _linear(lambda main: flatten(main, tmp_path, max_chars=100 * MB), make)


@pytest.mark.parametrize(
    "unit",
    [
        "\\begin{tikzpicture}x\n",
        "\\iffalse x\n",
        "\\begin{thebibliography}{9}x\n",
        "\\begin{comment}x\n",
        "\\begin{abstract}x\n",
    ],
)
def test_unclosed_regions_are_scanned_in_linear_time(unit):
    def make(n):
        return DOC % (unit * n)

    _linear(approx_text_length, make)
    _linear(rewrite_bibliography, make)
    fallback = PandocLatexConverter(LatexConfig(pandoc_path="/nonexistent/pandoc"))
    _linear(fallback._plaintext, make)  # \iffalse, comment and abstract lookups


@pytest.mark.parametrize(
    "text",
    [
        "a \\iffalse b \\fi c \\iffalse d \\fi e",
        "\\iffalse never closed \\iffalse x",
        "x \\begin{tikzpicture} p \\begin{axis} q \\end{axis} r \\end{tikzpicture} y",
        "\\begin{tikzpicture} unclosed \\begin{axis} closed \\end{axis} z",
        "\\begin{comment} c \\end{comment} \\begin{tikzpicture} \\begin{comment} d",
    ],
)
def test_regions_match_the_lazy_regex(text):
    envs = r"tikzpicture|axis|comment"
    lazy = re.sub(rf"\\begin\{{({envs})\}}.*?\\end\{{\1\}}", "#", text, flags=re.S)
    lazy = re.sub(r"\\iffalse\b.*?\\fi\b", "#", lazy, flags=re.S)
    begin = re.compile(rf"\\begin\{{({envs})\}}")
    linear = sub_regions(text, begin, lambda b: re.compile(rf"\\end\{{{b[1]}\}}"), lambda _: "#")
    linear = sub_regions(linear, re.compile(r"\\iffalse\b"), re.compile(r"\\fi\b"), lambda _: "#")
    assert linear == lazy
    assert list(regions("no regions", begin, begin)) == []


# ------------------------------------------------------ pylatexenc fallback
def test_fallback_keeps_monospace_and_verbatim_text(tmp_path):
    body = (
        "\\section{Setup}\n"
        "We fine-tune \\texttt{Llama-3-8B} on \\textsf{HumanEval} with \\verb|lr=3e-4| using\n"
        "\\begin{verbatim}\npython train.py --epochs 3\n\\end{verbatim}\n"
        "and compare against \\texttt{GPT-4} and \\mbox{Claude}. Parity is "
        "\\lstinline!n % 2 == 0!.\n"
        "\\begin{lstlisting}[language=Python]\nx = 7 % 2  # odd\n\\end{lstlisting}\n"
        "\\begin{minted}{python}\nprint('hi')\n\\end{minted}\n"
        "\\begin{comment}\nhidden remark\n\\end{comment}\n"
    )
    (tmp_path / "main.tex").write_text(DOC % body)
    conv = PandocLatexConverter(LatexConfig(pandoc_path=str(tmp_path / "missing"))).convert(
        tmp_path / "main.tex"
    )
    assert conv.method == PageMethod.LATEX_PLAINTEXT
    text = "\n".join(s.text for s in conv.segments)
    for kept in [
        "We fine-tune Llama-3-8B on HumanEval with lr=3e-4 using",
        "python train.py --epochs 3",
        "and compare against GPT-4 and Claude.",
        "Parity is n % 2 == 0.",
        "x = 7 % 2  # odd",
        "print('hi')",
    ]:
        assert kept in text
    for dropped in ["language=Python", "pythonprint", "hidden remark"]:
        assert dropped not in text


# ------------------------------------------------------------------ decoding
def test_decode_tex_reads_only_the_stray_bytes_as_legacy():
    utf8 = "Schrödinger, Gödel and naïve café results.\n".encode() * 3
    assert decode_tex(utf8 + b"Price: 5\xa0EUR \x93quoted\x94\n") == (
        "Schrödinger, Gödel and naïve café results.\n" * 3
        + "Price: 5\u00a0EUR \u201cquoted\u201d\n"
    )
    assert decode_tex("é".encode() + b" \x81") == "é \x81"  # undefined in cp1252: latin-1
    assert decode_tex(b"na\xefve caf\xe9") == "naïve café"  # no UTF-8 at all: legacy file


@pytest.mark.parametrize("head", ["\\def\\a{x ", "\\newcommand{\\b}{y ", "\\section{z "])
def test_unbalanced_definitions_and_sections_scan_in_linear_time(head):
    from docingest.adapters.converters.latex_source import macro_definitions, split_sections

    def make(n):
        return head * n

    _linear(lambda text: list(macro_definitions(text)), make)
    _linear(lambda text: split_sections(text, 2), make)


@pytest.mark.parametrize(
    "text",
    ["{a{b}c}", "\\{ {x} \\}", "{{}", "}{a}", "{a\\}b}", "x{" + "y" * 50 + "}"],
)
def test_brace_pairs_matches_match_brace(text):
    from docingest.adapters.converters.latex_source import brace_pairs, match_brace

    pairs = brace_pairs(text)
    for i, c in enumerate(text):
        if c == "{" and (i == 0 or text[i - 1] != "\\"):
            assert pairs.get(i) == match_brace(text, i), (text, i)
