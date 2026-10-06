"""Per-model OCR profiles: prompt, input size and output clean-up.

Shared by every OCR adapter (MLX in-process, OpenAI-compatible HTTP), so a model
behaves the same whichever runtime serves it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

# Models wrap output in a code fence, often without closing it (truncated or just
# sloppy), and sometimes label it yaml when it opens with front matter: strip the
# opening and closing fence lines independently.
_OPEN_FENCE = re.compile(r"\A\s*```[ \t]*(?:markdown|md|yaml|yml|json|text)?[ \t]*\n")
_CLOSE_FENCE = re.compile(r"\n?```\s*\Z")
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
# olmOCR front matter keys; the model sometimes drops the '---' delimiters, merges the
# closing one with the next line ('------') or turns a key into a heading.
_OLMOCR_KEYS = (
    r"(?:primary[_ ]language|is[_ ]rotation[_ ]valid|rotation[_ ]correction"
    r"|is[_ ]table|is[_ ]diagram)"
)
_FRONT_MATTER_LINE = re.compile(rf"^\s*(?:#+\s*)?{_OLMOCR_KEYS}\s*:.*$", re.IGNORECASE)
_NANONETS_TAGS = re.compile(r"</?(signature)>")
# Page furniture the model itself marked up: drop the block, not only the tags.
_NANONETS_FURNITURE = re.compile(
    r"<(page_number|watermark|header|footer)>.*?</\1>", re.DOTALL | re.IGNORECASE
)

GENERIC_PROMPT = (
    "Transcribe this document page to clean Markdown. Preserve reading order, "
    "headings (#), paragraphs, lists, and tables (Markdown tables). Render math "
    "as LaTeX ($...$). Omit page headers/footers and line-break hyphenation. "
    "Output only the transcription, no commentary."
)

# allenai/olmocr `build_no_anchoring_v4_yaml_prompt` (olmOCR-2 training prompt).
OLMOCR_PROMPT = (
    "Attached is one page of a document that you must process. Just return the plain "
    "text representation of this document as if you were reading it naturally. Convert "
    "equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying "
    "values for the primary_language, is_rotation_valid, rotation_correction, is_table, "
    "and is_diagram parameters."
)

NANONETS_PROMPT = (
    "Extract the text from the above document as if you were reading it naturally. "
    "Return the tables in html format. Return the equations in LaTeX representation. "
    "If there is an image in the document and image caption is not present, add a small "
    "description of the image inside the <img></img> tag; otherwise, add the image caption "
    "inside <img></img>. Watermarks should be wrapped in brackets. "
    "Ex: <watermark>OFFICIAL COPY</watermark>. Page numbers should be wrapped in brackets. "
    "Ex: <page_number>14</page_number> or <page_number>9/22</page_number>. "
    "Prefer using ☐ and ☑ for check boxes."
)


def _strip_common(text: str) -> str:
    text = _THINK.sub("", text).strip()
    if _OPEN_FENCE.match(text):
        text = _CLOSE_FENCE.sub("", _OPEN_FENCE.sub("", text, count=1)).strip()
    return text


def _olmocr(text: str) -> str:
    """Drop the olmOCR front matter block (keys + '---' delimiters) at the top."""
    lines = _strip_common(text).split("\n")
    i = 0
    while i < len(lines) and (
        not lines[i].strip()
        or set(lines[i].strip()) == {"-"}  # '---', or a merged '------'
        or _FRONT_MATTER_LINE.match(lines[i])
    ):
        i += 1
    return "\n".join(lines[i:]).strip()


def _nanonets(text: str) -> str:
    text = _NANONETS_FURNITURE.sub("", _strip_common(text))
    return _NANONETS_TAGS.sub("", text).strip()


def _olmocr_valid(raw: str) -> bool:
    """The olmOCR pipeline only accepts output that starts with its front matter
    (allenai/olmocr pipeline.py); anything else is retried up the temperature ladder."""
    head = _strip_common(raw).split("\n", 8)[:8]
    return sum(bool(_FRONT_MATTER_LINE.match(line)) for line in head) >= 3


# allenai/olmocr pipeline.py: TEMPERATURE_BY_ATTEMPT, repetition penalty unchanged.
OLMOCR_LADDER = tuple((t, 1.05) for t in (0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0))


@dataclass(frozen=True)
class OcrProfile:
    name: str
    prompt: str
    max_side: int  # longest image side fed to the model (px)
    postprocess: Callable[[str], str] = field(default=_strip_common)
    # Extra kwargs for the chat template, e.g. GLM-OCR's official prompt needs
    # enable_thinking=True (mlx-vlm otherwise appends "/nothink").
    chat_kwargs: dict = field(default_factory=dict)
    max_tokens: int = 4096  # model-card recommendation for a full page
    repetition_penalty: float | None = 1.05
    # How the model's authors run it (a fair comparison uses each model's own pipeline):
    prompt_first: bool = False  # text before the image in the user turn (olmOCR training)
    ladder: tuple[tuple[float, float | None], ...] | None = None  # (temperature, penalty)
    valid: Callable[[str], bool] | None = None  # raw output acceptable? else retry
    # The engine fingerprints record this, not the code of postprocess / valid: bump it
    # when an edit can change their results, so cached OCR output is redone. A test pins
    # each built-in profile's code (tests/unit/test_ocr_profiles.py).
    code_version: int = 1


# Sources: model cards + official repos (allenai/olmocr prompts.py, zai-org/GLM-OCR
# config.yaml, Nanonets-OCR2 card, PaddleOCR-VL-1.6 card), checked 2026-09-24.
PROFILES: dict[str, OcrProfile] = {
    "markdown": OcrProfile("markdown", GENERIC_PROMPT, 1600),  # Qwen3-VL, Qwen3.5
    "olmocr": OcrProfile(
        "olmocr",
        OLMOCR_PROMPT,
        1288,
        _olmocr,
        max_tokens=8000,
        prompt_first=True,
        ladder=OLMOCR_LADDER,
        valid=_olmocr_valid,
    ),
    "nanonets": OcrProfile("nanonets", NANONETS_PROMPT, 1600, _nanonets, max_tokens=8000),
    "glm-ocr": OcrProfile(
        "glm-ocr",
        "Text Recognition:",
        1600,
        chat_kwargs={"enable_thinking": True},
        max_tokens=8192,
        repetition_penalty=1.1,
    ),
    "paddleocr-vl": OcrProfile("paddleocr-vl", "OCR:", 1600),  # processor caps ~1 MP itself
}


def profile_for(
    name: str, prompt_override: str | None = None, max_side: int | None = None
) -> OcrProfile:
    try:
        base = PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown OCR profile {name!r}; choose from {sorted(PROFILES)}") from None
    return replace(base, prompt=prompt_override or base.prompt, max_side=max_side or base.max_side)


def attempts(
    profile: OcrProfile, temperature: float, penalty: float | None
) -> list[tuple[float, float | None]]:
    """The retry ladder: the profile's own, else greedy first, then a little sampling
    and a stronger repetition penalty (a max_tokens hit usually means a loop)."""
    if profile.ladder:
        return list(profile.ladder)
    return [
        (temperature, penalty),
        (0.2, max(penalty or 1.0, 1.15)),
        (0.5, max(penalty or 1.0, 1.25)),
    ]


def acceptable(profile: OcrProfile, raw: str, finish_reason: str | None) -> bool:
    """Stop retrying: not truncated, some text left after clean-up, and valid for the
    profile (e.g. olmOCR front matter present)."""
    if finish_reason == "length":
        return False
    if not any(c.isalnum() for c in profile.postprocess(raw)):
        return False
    return profile.valid is None or profile.valid(raw)
