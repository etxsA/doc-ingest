"""PaperQA2 integration (optional ``qa`` extra).

Two ways to plug the normalization layer into PaperQA2:

1. ``ask_corpus``: feed the already-normalized corpus (``data/normalized``) to
   ``Docs.aadd_texts`` -- works for *every* input type, incl. images and scans.
2. ``parse_pdf_to_pages``: drop-in for ``settings.parsing.parse_pdf`` so PaperQA2's
   own PDF ingestion routes each page through our text-layer / VLM-OCR router.

Everything runs locally: the LLM is served by ``mlx_vlm.server`` (OpenAI-compatible)
and embeddings use sentence-transformers, both pinned to Hugging Face commits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import PipelineConfig, load_config
from .pipeline import VARIANTS_DIR, Pipeline, _load_manifest, split_pages
from .schema import DocumentManifest

LLM_BASE = os.environ.get("DOCINGEST_LLM_BASE", "http://127.0.0.1:8080/v1")


def _quiet_litellm() -> None:
    import logging

    import litellm

    litellm.suppress_debug_info = True  # local models have no price entry -> noisy warnings
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)


def local_settings(cfg: PipelineConfig | None = None):
    from paperqa import Settings

    from .models import embedding_path, llm_path

    _quiet_litellm()
    cfg = cfg or load_config()
    # The server (scripts/serve_llm.sh) loads the same pinned local snapshot path, so the
    # request's model id matches what it serves and nothing is fetched from `main`.
    llm = os.environ.get("DOCINGEST_LLM") or llm_path(cfg)
    embedding = os.environ.get("DOCINGEST_EMBEDDING") or f"st-{embedding_path(cfg)}"
    llm_cfg = {
        "model_list": [
            {
                "model_name": "local",
                "litellm_params": {
                    "model": f"openai/{llm}",
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
        embedding=embedding,
    )
    s.parsing.use_doc_details = False  # no network metadata lookups
    s.parsing.multimodal = False
    # all-MiniLM-L6-v2 only embeds the first 256 tokens of a chunk: keep chunks small.
    s.parsing.reader_config = {"chunk_chars": cfg.qa.chunk_chars, "overlap": cfg.qa.overlap}
    s.answer.evidence_k = cfg.qa.evidence_k
    s.answer.answer_max_sources = 3
    s.answer.max_concurrent_requests = 2
    return s


def _corpus_manifests(normalized_dir: Path, warn) -> list[tuple[DocumentManifest, Path]]:
    """One manifest per document: the canonical run, else its most complete variant."""
    best: dict[str, tuple[DocumentManifest, Path]] = {}
    variants = sorted((normalized_dir / VARIANTS_DIR).glob("*/manifest.json"))
    for manifest_path in [*sorted(normalized_dir.glob("*/manifest.json")), *variants]:
        m = _load_manifest(manifest_path)
        if m is None:
            warn(f"skipped unreadable manifest {manifest_path} (re-run `docingest ingest`)")
            continue
        seen = best.get(m.doc_id)
        if seen is None or (VARIANTS_DIR in seen[1].parts and m.n_pages > seen[0].n_pages):
            best[m.doc_id] = (m, manifest_path.parent)
    for m, out in best.values():
        if VARIANTS_DIR in out.parts:
            warn(f"{m.source_name}: only a partial run exists ({m.n_pages}/{m.source_pages} pages)")
    return list(best.values())


def _token_windows(texts, tokenizer, max_tokens: int, overlap_tokens: int = 32):
    """Re-split chunks longer than the embedder's window so no text goes unembedded."""
    from paperqa.types import Text

    out = []
    for t in texts:
        enc = tokenizer(t.text, add_special_tokens=False, return_offsets_mapping=True)
        offsets = enc["offset_mapping"]
        if len(offsets) <= max_tokens:
            out.append(t)
            continue
        step = max_tokens - overlap_tokens
        for start in range(0, len(offsets), step):
            window = offsets[start : start + max_tokens]
            piece = t.text[window[0][0] : window[-1][1]]
            out.append(Text(text=piece, name=t.name, doc=t.doc))  # keeps its page-range name
            if start + max_tokens >= len(offsets):
                break
    return out


async def ask_corpus(
    question: str, normalized_dir: Path, cfg: PipelineConfig | None = None, warn=print
) -> str:
    from paperqa import Docs
    from paperqa.readers import chunk_pdf
    from paperqa.types import Doc, ParsedMetadata, ParsedText
    from transformers import AutoTokenizer

    from .models import embedding_path

    cfg = cfg or load_config()
    settings = local_settings(cfg)
    embedding_model = settings.get_embedding_model()  # load sentence-transformers once
    emb_dir = Path(embedding_path(cfg))
    tokenizer = AutoTokenizer.from_pretrained(emb_dir)
    st_cfg = json.loads((emb_dir / "sentence_bert_config.json").read_text())
    max_tokens = st_cfg["max_seq_length"] - 2  # [CLS] and [SEP]
    rc = settings.parsing.reader_config

    docs = Docs()
    corpus = _corpus_manifests(normalized_dir, warn)
    if not corpus:
        raise RuntimeError(f"no ingested documents in {normalized_dir}; run `docingest ingest` first")
    for m, out in corpus:
        pages = split_pages((out / "document.md").read_text())
        parsed = ParsedText(
            # chunk_pdf concatenates pages as-is: keep a break so words don't fuse across pages
            content={str(n): text + "\n\n" for n, text in pages.items()},
            metadata=ParsedMetadata(
                parsing_libraries=["docingest"],
                total_parsed_text_length=sum(map(len, pages.values())),
            ),
        )
        partial = "" if m.n_pages == m.source_pages else f", pages 1-{m.n_pages} of {m.source_pages}"
        doc = Doc(
            docname=m.doc_id[:16],
            dockey=m.doc_id,
            citation=f"{m.title} ({m.source_name}{partial})",
        )
        # Page-aware chunks: citations point at page ranges ("pages 3-4").
        texts = chunk_pdf(parsed, doc, chunk_chars=rc["chunk_chars"], overlap=rc["overlap"])
        texts = _token_windows(texts, tokenizer, max_tokens)
        await docs.aadd_texts(texts, doc, settings=settings, embedding_model=embedding_model)
    session = await docs.aquery(question, settings=settings, embedding_model=embedding_model)
    return session.formatted_answer


def parse_pdf_to_pages(
    path: str | os.PathLike,
    page_size_limit: int | None = None,
    page_range: int | tuple[int, int] | None = None,
    **kwargs,
):
    """``settings.parsing.parse_pdf`` hook: PaperQA2 PDF reader backed by docingest."""
    import pypdfium2 as pdfium
    from paperqa.types import ParsedMetadata, ParsedText
    from paperqa.utils import ImpossibleParsingError

    try:
        manifest, out = Pipeline(load_config(), log=lambda _: None).ingest(Path(path))
    except pdfium.PdfiumError as exc:  # corrupt / encrypted: PaperQA skips, doesn't retry
        raise ImpossibleParsingError(f"docingest could not open PDF {path}: {exc}") from exc
    pages = split_pages((out / "document.md").read_text())
    content = {}
    for rec in manifest.pages:
        n = rec.index + 1
        if isinstance(page_range, int) and n != page_range:
            continue
        if isinstance(page_range, tuple) and not page_range[0] <= n <= page_range[1]:
            continue
        text = pages.get(n, "")
        if page_size_limit and len(text) > page_size_limit:
            raise ImpossibleParsingError(  # same contract as PaperQA2's own readers
                f"The text in page {n} ({rec.method.value}) of {manifest.n_pages} was"
                f" {len(text)} chars long, which exceeds the {page_size_limit} char limit"
                f" for the PDF at path {path}."
            )
        content[str(n)] = text + "\n"  # page break survives PaperQA's concatenation
    methods = sorted({r.method.value for r in manifest.pages})
    return ParsedText(
        content=content,
        metadata=ParsedMetadata(
            parsing_libraries=[f"docingest ({', '.join(methods)})"],
            total_parsed_text_length=sum(len(t) for t in content.values()),
        ),
    )
