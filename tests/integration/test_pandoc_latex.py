"""PandocLatexConverter with the real pandoc (pypandoc-binary) and pylatexenc.

The fixture paper (tests/fixtures/latex/paper) packs the traps seen in arXiv sources:
a latin-1 include, a file that includes itself, a local .sty whose layout macro makes
pandoc loop forever, a natbib .bbl, a commented-out include and verbatim code.
"""

import gzip
import io
import os
import tarfile
import time
from pathlib import Path

import pytest
from fakes import FakeOcr

from docingest.adapters.converters.pandoc_latex import PandocLatexConverter
from docingest.bootstrap import Container
from docingest.config import LatexConfig
from docingest.domain.errors import ConversionError
from docingest.domain.models import PageMethod, SourceKind
from docingest.ports import DocumentConverter

FIXTURES = Path(__file__).parents[1] / "fixtures" / "latex"
PAPER = FIXTURES / "paper"
TITLES = [
    "Abstract",
    "Introduction",
    "Method",
    "Method > Complexity",
    "Experiments > Setup",
    "Conclusion",
    "References",
]
DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"

CONVERTER = PandocLatexConverter()
needs_pandoc = pytest.mark.skipif(CONVERTER.pandoc_version is None, reason="no pandoc binary")


def pack(dest: Path, files: dict[str, bytes], mode: str = "w:gz") -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    dest.write_bytes(buf.getvalue())
    return dest


def paper_files() -> dict[str, bytes]:
    return {
        p.relative_to(PAPER).as_posix(): p.read_bytes() for p in PAPER.rglob("*") if p.is_file()
    }


def text_of(conv) -> str:
    return "\n\n".join(s.text for s in conv.segments)


def test_fingerprint_names_every_setting():
    assert isinstance(CONVERTER, DocumentConverter)
    fp = CONVERTER.fingerprint
    assert f"pandoc {CONVERTER.pandoc_version or 'unavailable'}" in fp
    assert "--sandbox" in fp and "split_level=2" in fp and "pylatexenc 2." in fp
    assert PandocLatexConverter(LatexConfig(split_level=1)).fingerprint != fp
    assert "no fallback" in PandocLatexConverter(LatexConfig(fallback=False)).fingerprint


@needs_pandoc
@pytest.mark.parametrize("form", ["tex", "tar.gz", "tar"])
def test_fixture_paper(tmp_path, form):
    if form == "tex":
        src = PAPER / "main.tex"
    else:  # arXiv archives: no extension, plus figures and a decoy full document
        extra = {"figs/plot.png": b"\x89PNG....", "supplement.tex": (DOC % "Decoy").encode()}
        src = pack(
            tmp_path / "2401.00001", {**paper_files(), **extra}, "w:gz" if form == "tar.gz" else "w"
        )
    conv = CONVERTER.convert(src)

    assert conv.method == PageMethod.LATEX and conv.engine == CONVERTER.engine
    assert [s.title for s in conv.segments] == TITLES
    assert conv.title == "Sparse Attention for Tiny Transformers"
    assert conv.metadata is not None
    assert conv.metadata.authors == ["Ada Lovelace", "Alan Turing"]
    assert (
        conv.metadata.abstract
        == "We study attention over $\\mathbf{x}\\in \\mathbb{R}^d$ and report a 2x speed-up."
    )
    assert conv.warnings == ["skipped \\input{sections/setup} (recursive include)"]
    text = text_of(conv)
    for expected in [
        "naïve attention is quadratic; see the café benchmark",  # latin-1 include
        "The score is SPARSE-SCORE computed as $$\\begin{equation}",  # .sty macro, display math
        "\\mathop{\\mathrm{softmax}}_j",  # \DeclareMathOperator from the .sty
        "It costs $O(n \\log n)$ instead of $O(n^2)$ [@vaswani2017; @child2019], i.e. 50% fewer",
        "    % this line is code, not a comment\n    \\input{not-an-include}",  # verbatim
        "| Sparse | 27.1 |  40M   |",
        "1.  [@child2019] Rewon Child, Scott Gray, Alec Radford, and Ilya Sutskever.",
        "In *Advances in Neural Information Processing Systems*, 2017.",  # {\em venue} kept
    ]:
        assert expected in text
    for absent in ["hidden remark", "Decoy", "urlstyle"]:
        assert absent not in text


@needs_pandoc
def test_single_gzipped_tex(tmp_path):
    body = "\\title{Old Paper}\\maketitle\n\\section{One} First $a+b$.\n\\section{Two} Second."
    src = tmp_path / "math0211159"
    src.write_bytes(gzip.compress((DOC % body).encode()))
    conv = CONVERTER.convert(src)
    assert [s.title for s in conv.segments] == ["One", "Two"]
    assert conv.title == "Old Paper" and "First $a+b$." in text_of(conv)


@needs_pandoc
def test_never_reads_outside_the_source(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-MARKER")
    paper = tmp_path / "paper"
    paper.mkdir()
    tricks = [
        f"\\lstinputlisting{{{secret}}}",
        "\\verbatiminput{../secret.txt}",
        "\\inputminted{text}{../secret.txt}",
        f"\\input{{{secret}}}",
        "\\include{../secret}",
        "\\newcommand{\\sneaky}{\\lstinputlisting}\\sneaky{../secret.txt}",
    ]
    (paper / "main.tex").write_text(DOC % "\n".join(["Visible text.", *tricks]))
    conv = CONVERTER.convert(paper / "main.tex")
    assert "Visible text." in text_of(conv)
    assert "TOP-SECRET" not in text_of(conv)


@needs_pandoc
def test_self_referential_style_macros_do_not_hang(tmp_path):
    # arXiv 2409.13740's style file does this; pandoc alone expands it forever.
    src = tmp_path / "main.tex"
    src.write_text(
        "\\documentclass{article}\n"
        "\\renewcommand{\\normalsize}{\\@setfontsize\\normalsize\\@xpt\\@xipt}\n"
        "\\begin{document}\n\\normalsize\n\\section{Fine}\nConverted quickly.\n\\end{document}\n"
    )
    t0 = time.perf_counter()
    conv = PandocLatexConverter(LatexConfig(timeout_s=30)).convert(src)
    assert time.perf_counter() - t0 < 10
    assert conv.method == PageMethod.LATEX and "Converted quickly." in text_of(conv)
    assert any("\\normalsize" in w for w in conv.warnings)


def fake_pandoc(tmp_path: Path, script: str) -> str:
    exe = tmp_path / "pandoc"
    exe.write_text(
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "pandoc 9.9"; exit 0; fi\n{script}\n'
    )
    exe.chmod(0o755)
    return str(exe)


@pytest.mark.skipif(os.name != "posix", reason="shell-script stand-in for pandoc")
def test_timeout_falls_back_to_plain_text(tmp_path):
    cfg = LatexConfig(timeout_s=1, pandoc_path=fake_pandoc(tmp_path, "exec sleep 20"))
    t0 = time.perf_counter()
    conv = PandocLatexConverter(cfg).convert(PAPER / "main.tex")
    assert time.perf_counter() - t0 < 10
    assert conv.method == PageMethod.LATEX_PLAINTEXT
    assert conv.engine.startswith("pylatexenc ")
    assert "pandoc timed out after 1s; used the pylatexenc plain-text fallback" in conv.warnings
    assert [s.title for s in conv.segments] == TITLES
    assert conv.metadata is not None and conv.metadata.authors == ["Ada Lovelace", "Alan Turing"]
    text = text_of(conv)
    assert "café" in text and "$O(n \\log n)$" in text and "[@vaswani2017; @child2019]" in text


@pytest.mark.skipif(os.name != "posix", reason="shell-script stand-in for pandoc")
def test_suspiciously_short_output_falls_back(tmp_path):
    stub = "printf '{}\\n@@docingest-body@@\\n# Only a heading\\n'"
    conv = PandocLatexConverter(LatexConfig(pandoc_path=fake_pandoc(tmp_path, stub))).convert(
        PAPER / "main.tex"
    )
    assert conv.method == PageMethod.LATEX_PLAINTEXT
    assert any("pandoc kept" in w for w in conv.warnings)


def test_fallback_survives_pylatexenc_crashes(tmp_path, monkeypatch):
    from pylatexenc.latex2text import LatexNodes2Text

    real = LatexNodes2Text.latex_to_text

    def flaky(self, latex, **kw):
        if "BOOM" in latex:
            raise IndexError("pylatexenc spec bug")
        return real(self, latex, **kw)

    monkeypatch.setattr(LatexNodes2Text, "latex_to_text", flaky)
    link = "\\href{https://x.org/a_b%20c}{site}"
    (tmp_path / "main.tex").write_text(
        DOC % f"\\section{{Fine}} ok {link}\n\\section{{Bad}} BOOM \\textbf{{bold}} words {link}"
    )
    no_pandoc = LatexConfig(pandoc_path=str(tmp_path / "missing"))
    conv = PandocLatexConverter(no_pandoc).convert(tmp_path / "main.tex")
    assert conv.method == PageMethod.LATEX_PLAINTEXT
    texts = {s.title: s.text for s in conv.segments}
    assert "ok site (https://x.org/a_b%20c)" in texts["Fine"]
    assert "BOOM bold words site (https://x.org/a_b%20c)" in texts["Bad"]
    assert "pylatexenc failed on 1 fragment(s); kept their bare text" in conv.warnings


@needs_pandoc
def test_malformed_latex(tmp_path):
    conv = CONVERTER.convert(FIXTURES / "malformed.tex")  # unclosed group and itemize
    assert conv.method == PageMethod.LATEX_PLAINTEXT
    assert [s.title for s in conv.segments] == ["Broken"]
    assert "pandoc exited with code 64" in conv.warnings[0]
    with pytest.raises(ConversionError, match="code 64"):
        PandocLatexConverter(LatexConfig(fallback=False)).convert(FIXTURES / "malformed.tex")


def test_missing_pandoc(tmp_path):
    conv_cfg = LatexConfig(pandoc_path=str(tmp_path / "no-such-pandoc"))
    converter = PandocLatexConverter(conv_cfg)
    assert "pandoc unavailable" in converter.fingerprint
    conv = converter.convert(PAPER / "main.tex")
    assert conv.method == PageMethod.LATEX_PLAINTEXT and conv.title is not None
    assert any("pandoc could not run" in w for w in conv.warnings)
    with pytest.raises(ConversionError, match="could not run"):
        PandocLatexConverter(conv_cfg.model_copy(update={"fallback": False})).convert(
            PAPER / "main.tex"
        )


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file permissions")
def test_unreadable_include_is_a_conversion_error(tmp_path):
    (tmp_path / "main.tex").write_text(DOC % "\\input{locked}")
    locked = tmp_path / "locked.tex"
    locked.write_text("secret")
    locked.chmod(0)
    try:
        with pytest.raises(ConversionError, match="cannot read LaTeX source"):
            CONVERTER.convert(tmp_path / "main.tex")
    finally:
        locked.chmod(0o644)


@needs_pandoc
def test_empty_document_is_an_error(tmp_path):
    (tmp_path / "main.tex").write_text(DOC % "% nothing but a comment")
    with pytest.raises(ConversionError, match="no text"):
        CONVERTER.convert(tmp_path / "main.tex")


@needs_pandoc
def test_split_level_one():
    conv = PandocLatexConverter(LatexConfig(split_level=1)).convert(PAPER / "main.tex")
    assert [s.title for s in conv.segments] == [
        "Abstract",
        "Introduction",
        "Method",
        "Experiments",
        "Conclusion",
        "References",
    ]


@needs_pandoc
def test_ingest_through_the_container(cfg, tmp_path):
    logs: list[str] = []
    svc = Container(cfg, log=logs.append, overrides={"ocr": FakeOcr()}).ingest
    src = pack(tmp_path / "2401.00001.tar.gz", paper_files())
    doc = svc.ingest(src)
    m = doc.manifest
    assert m.source_kind == SourceKind.LATEX and m.complete
    assert m.title == "Sparse Attention for Tiny Transformers"
    assert m.metadata is not None and m.metadata.authors == ["Ada Lovelace", "Alan Turing"]
    assert [p.title for p in m.pages] == TITLES
    assert {p.method for p in m.pages} == {PageMethod.LATEX}
    assert all(p.engine == CONVERTER.engine for p in m.pages)
    markdown = svc.store.markdown(doc)
    assert "$O(n \\log n)$" in markdown and "<!-- page 7 | method=latex -->" in markdown
    assert any("recursive include" in line for line in logs)
    assert svc.ingest(src).location == doc.location and logs[-1].startswith("[cache]")


# Real arXiv e-prints (not redistributable, so not in the repo): point
# DOCINGEST_LATEX_SAMPLES at a folder of raw https://arxiv.org/src/<id> downloads.
SAMPLES = Path(os.environ.get("DOCINGEST_LATEX_SAMPLES", "/nonexistent"))


@needs_pandoc
@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "title", "min_segments"),
    [
        ("1706.03762.src", "Attention Is All You Need", 15),
        (
            "2409.13740.src",
            "Language agents achieve superhuman synthesis of scientific knowledge",
            10,
        ),
        ("2303.08774.src", "GPT-4 Technical Report", 20),
        (
            "math_0211159.src",
            "The entropy formula for the Ricci flow and its geometric applications",
            10,
        ),
    ],
)
def test_real_arxiv_sources(name, title, min_segments):
    src = SAMPLES / name
    if not src.is_file():
        pytest.skip(f"set DOCINGEST_LATEX_SAMPLES to a folder with {name}")
    t0 = time.perf_counter()
    conv = CONVERTER.convert(src)
    assert time.perf_counter() - t0 < 30
    assert conv.method == PageMethod.LATEX and conv.title == title
    assert len(conv.segments) >= min_segments
    assert text_of(conv).count("$") >= 20  # math survives as $...$
