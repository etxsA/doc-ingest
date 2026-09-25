"""Per-model OCR profiles: prompt, input size and output clean-up.

Shared by every OCR adapter (MLX in-process, OpenAI-compatible HTTP), so a model
behaves the same whichever runtime serves it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# Models wrap output in a code fence, often without closing it (truncated or just
# sloppy), and sometimes label it yaml when it opens with front matter: strip the
# opening and closing fence lines independently.
_OPEN_FENCE = re.compile(r"\A\s*```[ \t]*(?:markdown|md|yaml|yml|text)?[ \t]*\n")
_CLOSE_FENCE = re.compile(r"\n?```\s*\Z")
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
# olmOCR front matter keys; the model sometimes drops the '---' delimiters, merges the
# closing one with the next line ('------') or turns a key into a heading.
_OLMOCR_KEYS = (
    r"(?:primary[_ ]language|is[_ ]rotation[_ ]valid|rotation[_ ]correction"
    r"|is[_ ]table|is[_ ]diagram)"
)
_FRONT_MATTER_LINE = re.compile(rf"^\s*(?:#+\s*)?{_OLMOCR_KEYS}\s*:.*$", re.IGNORECASE)
_NANONETS_TAGS = re.compile(r"</?(page_number|watermark|signature)>")

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
    return _NANONETS_TAGS.sub("", _strip_common(text)).strip()


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


# Sources: model cards + official repos (allenai/olmocr prompts.py, zai-org/GLM-OCR
# config.yaml, Nanonets-OCR2 card, PaddleOCR-VL-1.6 card), checked 2026-09-24.
PROFILES: dict[str, OcrProfile] = {
    "markdown": OcrProfile("markdown", GENERIC_PROMPT, 1600),  # Qwen3-VL, Qwen3.5
    "olmocr": OcrProfile("olmocr", OLMOCR_PROMPT, 1288, _olmocr, max_tokens=8000),
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
    return OcrProfile(
        base.name,
        prompt_override or base.prompt,
        max_side or base.max_side,
        base.postprocess,
        base.chat_kwargs,
        base.max_tokens,
        base.repetition_penalty,
    )
