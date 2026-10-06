r"""OCR quality metrics against reference text, with bootstrap confidence intervals.

Markdown syntax, hyphenation and reading order inflate raw CER, so several views
are reported: CER/WER on normalized text, order-insensitive word F1, and a
character 3-gram F1 that tolerates reordering but still penalizes misspellings.

Normalization is applied to reference and hypothesis alike, so a formatting choice
never costs a candidate anything: Markdown / HTML markup, figure placeholders
(``![alt](src)`` and ``<img>description</img>`` both go), hyphenation, and LaTeX math
(``$d_k$`` scores like the text layer's ``dk``, ``\alpha`` like ``α``). Page furniture
(running headers, page numbers, arXiv margin stamps) is handled by ``strip_furniture``,
which needs the whole document to recognise running headers.
"""

from __future__ import annotations

import html
import math
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Sequence

_MD = re.compile(r"[#*_`>|\[\]\\$^{}]|<!--.*?-->|-{3,}", re.DOTALL)


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\s]*\)")  # one line: never swallow text
# Nanonets-style figure placeholder: the description is the model's, not page text, so it
# goes like the alt text of ![alt](src). A caption the model put inside <img> goes too.
_HTML_IMAGE = re.compile(r"<img\b[^<>]*>.*?</img\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(
    r"</?(?:table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col|br|hr|p|div|span|img|"
    r"sup|sub|b|i|u|em|strong|ul|ol|li|h[1-6]|figure|figcaption|page_number|watermark|"
    r"signature)\b[^<>]*/?>",
    re.IGNORECASE,
)


# Found by the benchmark's failure analysis (scoring v3): model-written image alt text
# without a closing "(src)", link targets, code-fence labels, OTSL table-cell tokens
# and olmOCR front matter were being scored as page text.
_MD_IMAGE_OPEN = re.compile(r"!\[[^\]\n]*(?:\]\([^)\n]*\)?)?")  # ![alt](src) even if unclosed
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")  # [text](url) -> text
_BARE_URL = re.compile(r"\(\s*https?://[^)\s]*\s*\)")
_FENCE_LINE = re.compile(r"^\s*```[\w-]*\s*$", re.MULTILINE)
_OTSL = re.compile(r"</?(?:fcel|ecel|lcel|ucel|xcel|nl|ched|rhed|srow|otsl|loc_\d+)>")
_FRONT_MATTER_KEY = re.compile(
    r"^\s*(?:#+\s*)?(?:primary[_ ]language|is[_ ]rotation[_ ]valid|rotation[_ ]correction|"
    r"is[_ ]table|is[_ ]diagram)\s*:.*$",
    re.IGNORECASE | re.MULTILINE,
)


def plain_text(markdown: str) -> str:
    """A transcription reduced to what a text layer can contain, for format-neutral CER."""
    text = _HTML_COMMENT.sub(" ", markdown)
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_IMAGE_OPEN.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub(" ", text)
    text = _HTML_IMAGE.sub(" ", text)
    text = _FENCE_LINE.sub(" ", text)
    text = _OTSL.sub(" ", text)
    text = _FRONT_MATTER_KEY.sub(" ", text)
    return html.unescape(_HTML_TAG.sub(" ", text))


# --------------------------------------------------------------------------- LaTeX math

# A PDF text layer spells math in Unicode; the markdown / olmocr / nanonets prompts ask
# for LaTeX. This deliberately small table maps what papers commonly use. Variant glyphs
# (\epsilon -> ϵ, \phi -> ϕ) fold to their plain letter under NFKC, like the text layer.


def _pairs(spec: str) -> dict[str, str]:
    """``"alpha α beta β"`` -> ``{"alpha": "α", "beta": "β"}``: compact symbol tables."""
    words = spec.split()
    return dict(zip(words[::2], words[1::2], strict=True))


_LATEX_SYMBOLS = _pairs(
    "alpha α beta β gamma γ delta δ epsilon ϵ varepsilon ε zeta ζ eta η theta θ vartheta ϑ "
    "iota ι kappa κ lambda λ mu μ nu ν xi ξ pi π varpi ϖ rho ρ varrho ϱ sigma σ varsigma ς "
    "tau τ upsilon υ phi ϕ varphi φ chi χ psi ψ omega ω Gamma Γ Delta Δ Theta Θ Lambda Λ "
    "Xi Ξ Pi Π Sigma Σ Upsilon Υ Phi Φ Psi Ψ Omega Ω "
    "cdot · times × div ÷ pm ± mp ∓ leq ≤ le ≤ geq ≥ ge ≥ neq ≠ ne ≠ approx ≈ sim ∼ "
    "simeq ≃ equiv ≡ propto ∝ infty ∞ partial ∂ nabla ∇ sum ∑ prod ∏ int ∫ sqrt √ "
    "to → rightarrow → leftarrow ← gets ← Rightarrow ⇒ Leftarrow ⇐ leftrightarrow ↔ "
    "Leftrightarrow ⇔ mapsto ↦ uparrow ↑ downarrow ↓ in ∈ notin ∉ ni ∋ subset ⊂ "
    "subseteq ⊆ supset ⊃ supseteq ⊇ cup ∪ cap ∩ emptyset ∅ varnothing ∅ forall ∀ "
    "exists ∃ neg ¬ lnot ¬ wedge ∧ land ∧ vee ∨ lor ∨ ldots … dots … cdots ⋯ prime ′ "
    "circ ∘ bullet • oplus ⊕ otimes ⊗ top ⊤ bot ⊥ perp ⊥ langle ⟨ rangle ⟩ lfloor ⌊ "
    "rfloor ⌋ lceil ⌈ rceil ⌉ mid | ell ℓ hbar ℏ star ⋆ ast ∗ dagger †"
)
# Layout and font scaffolding: the command goes, its argument's text stays.
_LATEX_DROP = frozenset(
    " ".join(
        (
            "left right big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr middle",
            "mathrm mathbf mathit mathsf mathtt mathcal mathbb mathfrak mathscr boldsymbol bm",
            "text textrm textbf textit texttt textsf textnormal emph operatorname mbox",
            "displaystyle textstyle scriptstyle limits nolimits frac dfrac tfrac cfrac binom",
            "hat bar tilde vec dot ddot check breve acute grave widehat widetilde overline",
            "underline overrightarrow mathring",
        )
    ).split()
)
_LATEX_SPACE = frozenset(("quad", "qquad", "enspace", "thinspace", "medspace", "thickspace"))
_LATEX_ESCAPES = {"_": "_", "%": "%", "&": "&", "#": "#", "$": "$", "{": "{", "}": "}"}
_LATEX_ESCAPES.update({"|": "‖", ",": " ", ";": " ", ":": " ", "!": " ", " ": " "})
_LATEX_RE = re.compile(
    r"\\(?P<gap>\\)"  # \\ line break
    r"|\\[()]"  # \( \) inline math delimiters
    r"|\\(?:left|right)\s*\."  # \left. \right. (invisible delimiter)
    r"|\\(?:begin|end)\s*\{[^{}]*\}"  # environments: aligned, array, pmatrix, ...
    r"|\\[hv]space\*?\s*\{[^{}]*\}"
    r"|\\(?P<cmd>[A-Za-z]+)\*?"
    r"|\\(?P<esc>[_%&#$\{\}|,;:! ])"
)
# Alignment tabs inside an environment are layout, not text.
_LATEX_ENV = re.compile(
    r"\\begin\s*\{(?P<env>[A-Za-z]+\*?)\}(?P<body>.*?)\\end\s*\{(?P=env)\}", re.S
)
# A sub/superscript marker after a base: the marker goes, the script joins the base
# ("d_{model}" -> "d{model}" -> "dmodel" once braces go).
_SCRIPT = re.compile(r"(?<=\S)[_^](?=[\w{])")
_MATH_MARKUP = re.compile(r"[${}]")  # delimiters and grouping: no text of their own


def _latex_token(m: re.Match[str]) -> str:
    if (cmd := m["cmd"]) is not None:
        if cmd in _LATEX_SYMBOLS:
            return _LATEX_SYMBOLS[cmd]
        if cmd in _LATEX_DROP:
            return ""
        return " " if cmd in _LATEX_SPACE else cmd  # \log, \max, ...: the name is the text
    if (esc := m["esc"]) is not None:
        return _LATEX_ESCAPES[esc]
    return " " if m["gap"] is not None else ""


def latex_to_text(text: str) -> str:
    r"""LaTeX math spelled the way a PDF text layer prints it; plain text passes through.

    ``$d_{model}$`` -> ``dmodel``, ``$\alpha = 0.3$`` -> ``α = 0.3``,
    ``\mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)`` -> ``softmax(QKT √dk)``.
    Symbols become Unicode, layout and font commands go (their argument stays),
    sub/superscripts join their base without a space (the text layer prints ``dk`` for
    d with subscript k), the two arguments of ``\frac`` read as two words, and ``$``
    and grouping braces are dropped rather than turned into spaces, so ``$d_k$,`` reads
    ``dk,``. Applied to reference and hypothesis alike by ``normalize``.
    """
    text = _LATEX_ENV.sub(lambda m: m["body"].replace("&", " "), text)
    text = _LATEX_RE.sub(_latex_token, text)
    text = _SCRIPT.sub("", text).replace("}{", " ")
    return _MATH_MARKUP.sub("", text)


def normalize(text: str) -> str:
    # LaTeX first: the Unicode it produces is folded by NFKC like the reference's.
    text = unicodedata.normalize("NFKC", latex_to_text(plain_text(text)))
    # Hyphenation is a formatting choice ("transduc-tion" / "transduction"): ignore it.
    text = re.sub(r"(\w)[-\x02]\s*(\w)", r"\1\2", text)
    text = _MD.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


# --------------------------------------------------------------------------- page furniture

FURNITURE_ZONE = 3  # non-empty lines at the top and at the bottom where furniture lives
# Matched against normalize()d lines, so Markdown emphasis or brackets do not matter.
_ARXIV_STAMP = re.compile(r"^arxiv:\d{4}\.\d{4,5}(?:v\d+)? \S+ \d{1,2} [a-z]{3,9}\.? \d{4}$")
_PAGE_NUMBER = re.compile(r"^(?:page )?[-–—]? ?\d{1,4} ?(?:(?:/|of) ?\d{1,4})? ?[-–—]?$")


def _zone(indices: Sequence[int], size: int = FURNITURE_ZONE) -> set[int]:
    return {*indices[:size], *indices[-size:]}


def running_lines(pages: Sequence[str], *, zone: int = FURNITURE_ZONE) -> frozenset[str]:
    """Normalized lines near the top or bottom of at least half of a document's pages
    (and of at least two): running headers and footers such as a paper's short title."""
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [n for line in page.splitlines() if (n := normalize(line))]
        counts.update({lines[i] for i in _zone(range(len(lines)), zone)})
    need = max(2, math.ceil(len(pages) / 2))
    return frozenset(
        line
        for line, c in counts.items()
        if c >= need and sum(ch.isalnum() for ch in line) >= 4 and not _PAGE_NUMBER.match(line)
    )


def strip_furniture(
    reference: str, hypothesis: str, running: Collection[str] = ()
) -> tuple[str, str]:
    """(reference, hypothesis) of one page without page furniture.

    From the reference: arXiv margin stamps anywhere, ``running`` lines near the top or
    bottom, and a bare page number as the first or last remaining line. From the
    hypothesis: arXiv stamps, and each line removed from the reference at most once when
    the same text sits near the hypothesis's top or bottom. Removing furniture from both
    sides means a model that follows "omit page headers/footers" and one that copies
    them score the same, and a line that is furniture only on other pages (a title that
    doubles as the running header) is never taken from this page's hypothesis.
    """
    ref_lines = reference.splitlines()
    norm = [normalize(line) for line in ref_lines]
    body = [i for i, n in enumerate(norm) if n]
    zone = _zone(body)
    drop = {i for i in body if _ARXIV_STAMP.match(norm[i]) or (i in zone and norm[i] in running)}
    rest = [i for i in body if i not in drop]
    drop |= {i for i in (rest[:1] + rest[-1:]) if _PAGE_NUMBER.match(norm[i])}
    removed = [norm[i] for i in sorted(drop) if not _ARXIV_STAMP.match(norm[i])]

    hyp_lines = hypothesis.splitlines()
    hyp_norm = [normalize(line) for line in hyp_lines]
    hyp_drop = {i for i, n in enumerate(hyp_norm) if _ARXIV_STAMP.match(n)}
    hyp_zone = _zone([i for i, n in enumerate(hyp_norm) if n and i not in hyp_drop])
    for furniture in removed:
        match = next((i for i in sorted(hyp_zone - hyp_drop) if hyp_norm[i] == furniture), None)
        if match is not None:
            hyp_drop.add(match)
    return (
        "\n".join(line for i, line in enumerate(ref_lines) if i not in drop),
        "\n".join(line for i, line in enumerate(hyp_lines) if i not in hyp_drop),
    )


def _bag_f1(ref: Counter[str], hyp: Counter[str]) -> float:
    overlap = sum((ref & hyp).values())
    if not overlap:
        return 0.0
    p, r = overlap / sum(hyp.values()), overlap / sum(ref.values())
    return 2 * p * r / (p + r)


def word_f1(ref: str, hyp: str) -> float:
    return _bag_f1(Counter(ref.split()), Counter(hyp.split()))


def _word_trigrams(text: str) -> Counter[str]:
    """Trigrams inside space-padded words: independent of word order, sensitive to spelling."""
    grams: Counter[str] = Counter()
    for word in text.split():
        padded = f" {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return grams


def char3_f1(ref: str, hyp: str) -> float:
    return _bag_f1(_word_trigrams(ref), _word_trigrams(hyp))


def score(reference: str, hypothesis: str) -> dict[str, float | None]:
    import jiwer

    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:  # jiwer would return a raw insertion count, not a rate
        return {
            "cer": None,
            "wer": None,
            "word_f1": None,
            "char3_f1": None,
            "ref_chars": 0,
            "hyp_chars": len(hyp),
        }
    return {
        "cer": round(min(jiwer.cer(ref, hyp), 1.0), 4),  # capped: runaway output -> 1.0
        "wer": round(min(jiwer.wer(ref, hyp), 1.0), 4),
        "word_f1": round(word_f1(ref, hyp), 4),
        "char3_f1": round(char3_f1(ref, hyp), 4),
        "ref_chars": len(ref),
        "hyp_chars": len(hyp),
    }


def bootstrap_ci(
    values: Sequence[float], *, n: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, low, high) percentile bootstrap CI of the mean. Deterministic via seed."""
    vals = [v for v in values if v is not None]
    if not vals:
        nan = float("nan")
        return nan, nan, nan
    rng = random.Random(seed)
    k = len(vals)
    means = sorted(sum(rng.choices(vals, k=k)) / k for _ in range(n))
    lo, hi = means[int(n * alpha / 2)], means[int(n * (1 - alpha / 2)) - 1]
    return sum(vals) / k, lo, hi
