"""Pure LaTeX-source helpers (latex_source) and the pandoc adapter's Markdown helpers.

No pandoc binary needed: everything here is string / file-system logic.
"""

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from docingest.adapters.converters.latex_source import (
    approx_text_length,
    decode_tex,
    document_body,
    drop_unsafe_macros,
    find_main,
    flatten,
    match_brace,
    package_macros,
    prepare_for_pandoc,
    rewrite_bibliography,
    split_sections,
    strip_comments,
    unpack,
)
from docingest.adapters.converters.pandoc_latex import (
    assemble,
    author_names,
    inline_text,
    markdown_metadata,
    pandoc_warnings,
    split_markdown,
)
from docingest.domain.errors import ConversionError

MB = 2**20
DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"


def write(root: Path, rel: str, text: str | bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text)
    return p


def flat(main: Path, **kw) -> tuple[str, list[str]]:
    out = flatten(main, main.parent, max_chars=kw.pop("max_chars", MB), **kw)
    return out.text, out.warnings


def tar_bytes(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ------------------------------------------------------------------ decoding
@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("café\r\n".encode(), "café\n"),
        (b"\xef\xbb\xbfbom", "bom"),
        (b"\x93quoted\x94 caf\xe9", "\u201cquoted\u201d café"),  # cp1252
        (b"caf\xe9 \x81", "café \x81"),  # 0x81 is undefined in cp1252 -> latin-1
    ],
)
def test_decode_tex_never_fails(raw, text):
    assert decode_tex(raw) == text


def test_strip_comments_like_tex():
    src = (
        "keep % drop this\n"
        "   % whole-line comment disappears\n"
        "50\\% stays \\\\% but this goes\n"
        "\\verb|%not a comment| and \\url{http://x.org/a%20b} % gone\n"
        "\\begin{verbatim}\n"
        "% code, not a comment\n"
        "\\end{verbatim}\n"
        "\\begin{lstlisting}x = 1 % inline listing\\end{lstlisting} % gone\n"
        "last%"
    )
    assert strip_comments(src) == (
        "keep %\n"
        "50\\% stays \\\\%\n"
        "\\verb|%not a comment| and \\url{http://x.org/a%20b} %\n"
        "\\begin{verbatim}\n"
        "% code, not a comment\n"
        "\\end{verbatim}\n"
        "\\begin{lstlisting}x = 1 % inline listing\\end{lstlisting} %\n"
        "last%"
    )


def test_document_body_and_braces():
    assert document_body(DOC % "Hello") == "\nHello\n"
    assert document_body("no document env") == "no document env"
    text = "\\x{a{b}\\}c}rest"
    assert text[: match_brace(text, 2)] == "\\x{a{b}\\}c}"
    assert match_brace("{never closed", 0) is None
    assert match_brace("abc", 0) is None


# ------------------------------------------------------------- macro hygiene
def test_drop_unsafe_macros_removes_loops_and_structure():
    src = (
        "\\newcommand{\\R}{\\mathbb{R}}\n"
        "\\renewcommand{\\small}{\\@setfontsize\\small\\@ixpt\\@xpt}\n"  # pandoc loops on this
        "\\renewcommand{\\section}{\\@startsection{section}{1}{\\z@}{1ex}{1ex}{\\bf}}\n"
        "\\def\\vx{\\mathbf{x}}\n"
        "\\def\\loop#1{\\loop{#1}}\n"
    )
    out, dropped = drop_unsafe_macros(src)
    assert sorted(dropped) == ["loop", "section", "small"]
    assert "\\newcommand{\\R}" in out and "\\def\\vx" in out
    assert "setfontsize" not in out and "startsection" not in out


def test_package_macros_keeps_notation_only():
    sty = (
        "\\ProvidesPackage{mine}\n\\RequirePackage{amsmath}\n"
        "\\newcommand{\\mymac}{EXPANDED}\n"
        "\\newcommand\\myvec[1]{\\boldsymbol{#1}}\n"
        "\\DeclareMathOperator*{\\argmax}{arg\\,max}\n"
        "\\def\\@internal{x}\n"
        "\\newcommand{\\uses}{\\@internal}\n"
        "\\renewcommand{\\normalsize}{\\@setfontsize\\normalsize\\@xpt\\@xipt}\n"
        "\\newtheorem{thm}{Theorem}[section]\n"
    )
    out = package_macros(sty)
    assert out.splitlines() == [
        "\\newcommand{\\mymac}{EXPANDED}",
        "\\newcommand\\myvec[1]{\\boldsymbol{#1}}",
        "\\DeclareMathOperator*{\\argmax}{arg\\,max}",
        "\\newtheorem{thm}{Theorem}[section]",
    ]


def test_rewrite_bibliography_keeps_venue_and_keys():
    bbl = (
        "\\begin{thebibliography}{10}\n"
        "\\providecommand{\\natexlab}[1]{#1}\n"
        "\\providecommand{\\url}[1]{\\texttt{#1}}\n"
        "\\expandafter\\ifx\\csname urlstyle\\endcsname\\relax\\fi\n"
        "\\bibitem[Ba et~al.(2016)]{ba2016}\nJ. Ba.\n\\newblock Layer norm.\n"
        "\\newblock {\\em arXiv}, 2016.\n"
        "\\bibitem{x2}\nSecond.\n"
        "\\end{thebibliography}"
    )
    out = rewrite_bibliography(bbl)
    assert "\\section*{References}" in out and "\\begin{enumerate}" in out
    assert "\\item \\cite{ba2016} \nJ. Ba." in out and "\\item \\cite{x2}" in out
    assert "{\\em arXiv}" in out and "newblock" not in out
    assert "\\providecommand{\\natexlab}" in out
    assert "urlstyle" not in out and "\\providecommand{\\url}" not in out
    assert "{10}" not in out


def test_prepare_for_pandoc():
    src = "\\author{A \\And B \\AND C}\\date{\\today}\\renewcommand{\\tiny}{\\tiny}"
    out, warnings = prepare_for_pandoc(src)
    assert out == "\\author{A \\and B \\and C}\\date{}"
    assert warnings and "\\tiny" in warnings[0]


# ------------------------------------------------------------------- flatten
def test_flatten_resolves_like_tex(tmp_path):
    main = write(
        tmp_path,
        "main.tex",
        DOC % "\\input{a}\n\\input b.tex\n\\include{chap/c}\n% \\input{commented}\n",
    )
    write(tmp_path, "a.tex", "AAA\n")
    write(tmp_path, "b.tex", "BBB\n")
    write(tmp_path, "chap/c.tex", "CCC \\input{chap/d}\n")  # relative to the main file
    write(tmp_path, "chap/d.tex", "DDD")
    write(tmp_path, "commented.tex", "SHOULD NOT APPEAR")
    out = flatten(main, tmp_path, max_chars=MB)
    assert "AAA\nBBB\n" in out.text
    assert "\nCCC DDD\n\n" in out.text  # \include starts a new paragraph
    assert "SHOULD NOT APPEAR" not in out.text
    assert out.files == ["a.tex", "b.tex", "chap/c.tex", "chap/d.tex"]
    assert out.warnings == []


def test_flatten_decodes_latin1_includes(tmp_path):
    main = write(tmp_path, "main.tex", DOC % "\\input{latin}")
    write(tmp_path, "latin.tex", "caf\u00e9 na\u00efve".encode("latin-1"))
    assert "café naïve" in flat(main)[0]


@pytest.mark.parametrize(
    "files",
    [
        {"main.tex": DOC % "\\input{main}"},  # includes itself
        {"main.tex": DOC % "\\input{a}", "a.tex": "A \\input{b}", "b.tex": "B \\input{a}"},
    ],
)
def test_flatten_breaks_include_cycles(tmp_path, files):
    for name, text in files.items():
        write(tmp_path, name, text)
    text, warnings = flat(tmp_path / "main.tex")
    assert "%docingest: skipped (recursive include)" in text
    assert len(warnings) == 1 and "recursive include" in warnings[0]


def test_flatten_never_leaves_the_source_tree(tmp_path):
    secret = write(tmp_path, "secret.tex", "TOP SECRET")
    root = tmp_path / "paper"
    main = write(root, "main.tex", DOC % f"\\input{{../secret}}\n\\input{{{secret}}}\n")
    text, warnings = flat(main)
    assert "TOP SECRET" not in text
    assert len(warnings) == 2 and all("outside the source tree" in w for w in warnings)


def test_flatten_missing_unresolvable_and_depth(tmp_path):
    main = write(tmp_path, "main.tex", DOC % "x \\input{nope} y\n\\input{\\figdir/f}\n\\input{l1}")
    for i in range(1, 5):
        write(tmp_path, f"l{i}.tex", f"L{i} \\input{{l{i + 1}}}")
    write(tmp_path, "l5.tex", "L5")
    text, warnings = flat(main, max_depth=2)
    assert "x %docingest: skipped (missing)\n y" in text
    assert "L1 L2" in text and "L3" not in text
    assert [w.split("(")[-1] for w in warnings] == [
        "missing)",
        "unresolvable)",
        "nesting deeper than 2)",
    ]


def test_flatten_skips_verbatim_and_honours_endinput(tmp_path):
    main = write(
        tmp_path,
        "main.tex",
        DOC % "\\begin{verbatim}\n\\input{x}\n\\end{verbatim}\n\\verb|\\input{x}|\n\\input{e}",
    )
    write(tmp_path, "x.tex", "XXX")
    write(tmp_path, "e.tex", "kept\n\\endinput\nignored by TeX")
    text, _ = flat(main)
    assert "XXX" not in text and text.count("\\input{x}") == 2
    assert "kept" in text and "ignored by TeX" not in text


def test_flatten_subfile_and_import(tmp_path):
    main = write(tmp_path, "main.tex", DOC % "\\subfile{parts/s}\n\\import{chapters/}{c1}")
    write(
        tmp_path,
        "parts/s.tex",
        "\\documentclass[../main.tex]{subfiles}\n\\begin{document}\nSUB\n\\end{document}",
    )
    write(tmp_path, "chapters/c1.tex", "C1 \\input{c2}")  # import: relative to chapters/
    write(tmp_path, "chapters/c2.tex", "C2")
    text, warnings = flat(main)
    assert "SUB" in text and "subfiles" not in text
    assert "C1 C2" in text and warnings == []


def test_flatten_inlines_local_package_macros_only(tmp_path):
    main = write(
        tmp_path,
        "main.tex",
        "\\usepackage[x]{amsmath,mine}\n\\usepackage{mine}\n" + DOC % "\\mymac",
    )
    write(
        tmp_path,
        "mine.sty",
        "\\newcommand{\\mymac}{M}\n\\renewcommand{\\small}{\\@setfontsize\\small}\n\\endinput\n"
        "\\newcommand{\\after}{A}\n",
    )
    out = flatten(main, tmp_path, max_chars=MB)
    assert out.text.startswith("\\usepackage[x]{amsmath}\n\\newcommand{\\mymac}{M}")
    assert "\\after" in out.text  # a .sty is not cut at \endinput (guards live there)
    assert "setfontsize" not in out.text and out.files == ["mine.sty"]
    assert out.text.count("\\mymac}") == 1 and "\\usepackage{mine}" not in out.text  # loaded once


def test_flatten_inlines_thebibliography_bbl_only(tmp_path):
    main = write(tmp_path, "main.tex", DOC % "\\bibliography{refs}")
    write(
        tmp_path, "main.bbl", "\\begin{thebibliography}{1}\\bibitem{k} Ref.\\end{thebibliography}"
    )
    assert "\\bibitem{k} Ref." in flat(main)[0]
    write(tmp_path, "main.bbl", "% $ biblatex auxiliary file $\n\\entry{k}{article}{}")
    assert "\\bibliography{refs}" in flat(main)[0]  # biblatex .bbl: left alone


def test_flatten_size_cap(tmp_path):
    main = write(tmp_path, "main.tex", DOC % "\\input{big}")
    write(tmp_path, "big.tex", "x" * 5000)
    with pytest.raises(ConversionError, match="max_archive_mb"):
        flatten(main, tmp_path, max_chars=4000)


# ----------------------------------------------------------------- find_main
def test_find_main_prefers_readme_hints(tmp_path):
    write(tmp_path, "paper.tex", DOC % "x")
    write(tmp_path, "supp.tex", DOC % "y")
    readme = {"spec_version": 1, "sources": [{"filename": "supp.tex", "usage": "toplevel"}]}
    write(tmp_path, "00README.json", json.dumps(readme))
    assert find_main(tmp_path)[0].name == "supp.tex"
    (tmp_path / "00README.json").unlink()
    write(tmp_path, "00README.XXX", "paper.tex toplevelfile\nsupp.tex ignore\n")
    assert find_main(tmp_path)[0].name == "paper.tex"


def test_find_main_heuristics(tmp_path):
    write(tmp_path, "sec.tex", "\\section{Only a fragment}")
    write(tmp_path, "fig.tex", "\\documentclass{standalone}\\begin{document}x\\end{document}")
    write(tmp_path, "old.tex", "% \\documentclass{article}\n\\begin{document}\\end{document}")
    write(tmp_path, "real.tex", DOC % "\\input{sec}")
    assert find_main(tmp_path) == (tmp_path / "real.tex", [])
    write(tmp_path, "sub/main.tex", DOC % "conventional name")
    assert find_main(tmp_path)[0] == tmp_path / "sub" / "main.tex"


def test_find_main_not_included_then_largest(tmp_path):
    write(tmp_path, "a.tex", DOC % "\\input{b}")
    write(tmp_path, "b.tex", DOC % ("long " * 100))  # a full document, but a's include
    assert find_main(tmp_path) == (tmp_path / "a.tex", [])
    write(tmp_path, "c.tex", DOC % ("longer " * 100))
    main, warnings = find_main(tmp_path)
    assert main.name == "c.tex" and "a.tex" in warnings[0]


def test_find_main_without_tex(tmp_path):
    write(tmp_path, "README.md", "nothing here")
    with pytest.raises(ConversionError, match=r"no \.tex"):
        find_main(tmp_path)


# -------------------------------------------------------------------- unpack
def test_unpack_plain_tex_is_its_own_root(tmp_path):
    tex = write(tmp_path, "paper.tex", DOC % "x")
    out = unpack(tex, tmp_path / "work", max_bytes=MB)
    assert (out.root, out.main) == (tmp_path, tex)
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("mode", ["w:gz", "w"])
def test_unpack_archive_skips_media(tmp_path, mode):
    data = tar_bytes({"main.tex": b"\\input{s/x}", "s/x.tex": b"x", "fig.png": b"\x89PNG"}, mode)
    archive = write(tmp_path, "2401.00001", data)  # arXiv names have no extension
    out = unpack(archive, tmp_path / "work", max_bytes=MB)
    assert out.main is None and out.root == tmp_path / "work"
    assert sorted(p.name for p in out.root.rglob("*") if p.is_file()) == ["main.tex", "x.tex"]


def test_unpack_single_gzipped_tex(tmp_path):
    src = write(tmp_path, "math0211159", gzip.compress((DOC % "Ricci").encode()))
    out = unpack(src, tmp_path / "work", max_bytes=MB)
    assert out.main == tmp_path / "work" / "main.tex"
    assert "Ricci" in out.main.read_text()


def test_unpack_rejects_path_traversal_and_links(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("../evil.tex", "/abs/evil.tex", "ok.tex"):
            info = tarfile.TarInfo(name)
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))
        link = tarfile.TarInfo("link.tex")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    archive = write(tmp_path, "x.tar.gz", buf.getvalue())
    out = unpack(archive, tmp_path / "work" / "src", max_bytes=MB)
    files = sorted(p.relative_to(out.root).as_posix() for p in out.root.rglob("*") if p.is_file())
    assert files == ["abs/evil.tex", "ok.tex"]  # the data filter makes absolute names relative
    assert not (tmp_path / "work" / "evil.tex").exists()
    assert len(out.warnings) == 1 and "../evil.tex" in out.warnings[0]


def test_unpack_refuses_bombs_and_garbage(tmp_path):
    big = write(tmp_path, "big.tar.gz", tar_bytes({"main.tex": b"0" * 3000}))
    with pytest.raises(ConversionError, match="max_archive_mb"):
        unpack(big, tmp_path / "w1", max_bytes=2000)
    big_gz = write(tmp_path, "big.gz", gzip.compress(b"\\section{x}" + b" " * 3000))
    with pytest.raises(ConversionError, match="max_archive_mb"):
        unpack(big_gz, tmp_path / "w2", max_bytes=2000)
    corrupt = write(tmp_path, "bad.gz", b"\x1f\x8b\x08\x00garbage")
    with pytest.raises(ConversionError, match="corrupt gzip"):
        unpack(corrupt, tmp_path / "w3", max_bytes=MB)


# ------------------------------------------------------------------ measures
def test_approx_text_length_ignores_markup():
    assert approx_text_length(DOC % "\\textbf{Hello} $x$ \\cite{k} world") == len("Hello x k world")
    tikz = "\\begin{tikzpicture}\\draw (0,0) -- (1,1);\\end{tikzpicture}"
    assert approx_text_length(DOC % f"Hi {tikz}") == 2


def test_split_sections_levels():
    body = "pre \\section{A} a \\subsection[short]{A.1} b \\subsubsection{deep} c \\section*{B} d"
    assert split_sections(body, 2) == [
        (0, None, "pre "),
        (1, "A", " a "),
        (2, "A.1", " b \\subsubsection{deep} c "),
        (1, "B", " d"),
    ]
    assert [p[1] for p in split_sections("\\chapter{C} \\section{S}", 1)] == [None, "C"]
    assert split_sections("no headings", 2) == [(0, None, "no headings")]


# ------------------------------------------------ Markdown helpers (pandoc_latex)
def test_split_markdown_ignores_math_and_code():
    md = (
        "intro\n# One\ntext\n$$\n# not a heading\n$$\n```\n# code\n```\n"
        "## One.a\nx\n### deep stays\n# Two {#sec}\n"
    )
    parts = split_markdown(md, 2)
    assert [(lvl, title) for lvl, title, _, _ in parts] == [
        (0, None),
        (1, "One"),
        (2, "One.a"),
        (1, "Two {#sec}"),
    ]
    assert "# not a heading" in parts[1][3] and "# code" in parts[1][3]
    assert "### deep stays" in parts[2][3]


def test_assemble_paths_and_heading_only_sections():
    parts = [
        (0, None, "", "front matter"),
        (1, "Model", "# Model", ""),  # directly followed by a sub-heading
        (2, "Attention", "## Attention", "softmax"),
        (1, "End", "# End", "bye"),
        (1, "Empty", "# Empty", "   "),
    ]
    segs = assemble(parts, abstract="We do X.")
    assert [(s.title, s.text) for s in segs] == [
        ("Abstract", "# Abstract\n\nWe do X."),
        (None, "front matter"),
        ("Model > Attention", "# Model\n\n## Attention\n\nsoftmax"),
        ("End", "# End\n\nbye"),
        ("Empty", "# Empty"),
    ]


ADA_ALAN = ["Ada Lovelace", "Alan Turing"]


@pytest.mark.parametrize(
    ("author", "names"),
    [  # pandoc's Markdown for common \author blocks (hard line break = backslash-newline)
        (["Ada Lovelace[^1]\\\nEngine Lab\\\n`ada@x.org`", "Alan Turing\\\nUniv"], ADA_ALAN),
        ("Ada Lovelace$^{1}$, Alan Turing$^{2}$\\\n$^1$Engine Lab", ADA_ALAN),
        (
            "Ada Lovelace$^{1}$Alan Turing$^{1,2}$\\\n\\\nGrace Hopper$^{2}$\\\n$^{1}$Lab",
            [*ADA_ALAN, "Grace Hopper"],
        ),
        ("Ada Lovelace and Alan Turing\\\nGoogle Brain", ADA_ALAN),
        ("OpenAI[^1]", ["OpenAI"]),
        (None, []),
    ],
)
def test_author_names(author, names):
    assert author_names(author) == names


def test_markdown_metadata_and_warnings():
    meta = markdown_metadata(
        {"title": "A [Title](#x)[^2]\\\nSplit", "author": ["Ada Lovelace"], "abstract": "Abs.\n"}
    )
    assert meta is not None
    assert (meta.title, meta.authors, meta.abstract) == ("A Title Split", ["Ada Lovelace"], "Abs.")
    assert markdown_metadata({}) is None
    assert inline_text("x[^1]  y") == "x y"
    stderr = (
        "[WARNING] Could not convert TeX math \\foo at line 3 column 1\n"
        "[WARNING] Could not convert TeX math \\foo at line 9 column 2\nnoise\n"
    )
    assert pandoc_warnings(stderr) == [
        "pandoc: 2 warning(s), e.g. Could not convert TeX math \\foo"
    ]
    assert pandoc_warnings("") == []
