"""PaperQA2 integration (optional ``qa`` extra).

Two ways to plug the normalization layer into PaperQA2:

1. ``ask_corpus``: feed the already-normalized Markdown (``data/normalized``)
   to ``Docs.aadd`` -- works for *every* input type, incl. images and scans.
2. ``parse_pdf_to_pages``: drop-in for ``settings.parsing.parse_pdf`` so PaperQA2's
   own PDF ingestion routes each page through our text-layer / VLM-OCR router.

Everything runs locally: the LLM is served by ``mlx_vlm.server`` (OpenAI-compatible)
and embeddings use sentence-transformers.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import load_config
from .pipeline import Pipeline
from .schema import DocumentManifest

LLM_MODEL = os.environ.get("DOCINGEST_LLM", "mlx-community/Qwen3-VL-4B-Instruct-4bit")
LLM_BASE = os.environ.get("DOCINGEST_LLM_BASE", "http://127.0.0.1:8080/v1")
EMBEDDING = os.environ.get("DOCINGEST_EMBEDDING", "st-all-MiniLM-L6-v2")


def _quiet_litellm() -> None:
    import logging

    import litellm

    litellm.suppress_debug_info = True  # local models have no price entry -> noisy warnings
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)


def local_settings():
    from paperqa import Settings

    _quiet_litellm()

    llm_cfg = {
        "model_list": [
            {
                "model_name": "local",
                "litellm_params": {
                    "model": f"openai/{LLM_MODEL}",
                    "api_base": LLM_BASE,
                    "api_key": "sk-local",
                    "max_tokens": 1024,
                },
            }
        ]
    }
    s = Settings(
        llm="local",
        llm_config=llm_cfg,
        summary_llm="local",
        summary_llm_config=llm_cfg,
        embedding=EMBEDDING,
    )
    s.parsing.use_doc_details = False  # no network metadata lookups
    s.parsing.multimodal = False
    s.answer.evidence_k = 6
    s.answer.answer_max_sources = 3
    s.answer.max_concurrent_requests = 2
    return s


def _page_texts(markdown: str) -> list[str]:
    return [chunk.split("-->", 1)[1].strip() for chunk in markdown.split("<!-- page ")[1:]]


async def ask_corpus(question: str, normalized_dir: Path) -> str:
    from paperqa import Docs

    settings = local_settings()
    docs = Docs()
    for manifest_path in sorted(normalized_dir.glob("*/manifest.json")):
        m = DocumentManifest.model_validate_json(manifest_path.read_text())
        md = manifest_path.with_name("document.md")
        # PaperQA2 picks its chunker by extension: .txt gets prose chunking, .md does not.
        txt = manifest_path.with_name("document.txt")
        txt.write_text(md.read_text())
        await docs.aadd(
            txt,
            citation=f"{m.title} ({m.source_name})",
            docname=m.doc_id[:16],
            settings=settings,
        )
    session = await docs.aquery(question, settings=settings)
    return session.formatted_answer


def parse_pdf_to_pages(
    path: str | os.PathLike,
    page_size_limit: int | None = None,
    page_range: int | tuple[int, int] | None = None,
    **kwargs,
):
    """``settings.parsing.parse_pdf`` hook: PaperQA2 PDF reader backed by docingest."""
    from paperqa.types import ParsedMetadata, ParsedText

    manifest, out = Pipeline(load_config(), log=lambda _: None).ingest(Path(path))
    pages = _page_texts((out / "document.md").read_text())
    content = {}
    for rec, text in zip(manifest.pages, pages, strict=True):
        n = rec.index + 1
        if isinstance(page_range, int) and n != page_range:
            continue
        if isinstance(page_range, tuple) and not page_range[0] <= n <= page_range[1]:
            continue
        if page_size_limit and len(text) > page_size_limit:
            text = text[:page_size_limit]
        content[str(n)] = text
    methods = sorted({r.method.value for r in manifest.pages})
    return ParsedText(
        content=content,
        metadata=ParsedMetadata(
            parsing_libraries=[f"docingest ({', '.join(methods)})"],
            total_parsed_text_length=sum(len(t) for t in content.values()),
        ),
    )
