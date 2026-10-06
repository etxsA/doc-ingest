"""QuestionAnswerer adapter: PaperQA2 over the normalized corpus (``qa`` extra).

The LLM is any litellm model: by default the pinned local Qwen3-VL snapshot served
by ``mlx_vlm.server`` (OpenAI-compatible), or e.g. ``ollama/llama3.1``. Embeddings
default to a pinned sentence-transformers snapshot; any PaperQA embedding string
(e.g. ``ollama/mxbai-embed-large``) can replace it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from ...config import DEFAULT_LLM_BASE, AppConfig
from ...domain.text import split_pages
from ...ports import StoredDocument
from ..models.huggingface import resolve


def _quiet_litellm() -> None:
    import logging

    import litellm

    litellm.suppress_debug_info = True  # local models have no price entry -> noisy warnings
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)


def llm_path(cfg: AppConfig) -> str:
    """Local snapshot path of the pinned QA LLM (default: the [ocr] model)."""
    rev = cfg.qa.llm_revision or cfg.ocr.revision
    assert rev is not None
    return resolve(cfg.ocr.repo_id, rev)


def embedding_path(cfg: AppConfig) -> str:
    return resolve(cfg.qa.embedding_repo_id, cfg.qa.embedding_revision)


def llm_params(cfg: AppConfig) -> dict:
    """litellm parameters of the QA LLM.

    The default model is the pinned local snapshot served by scripts/serve_llm.sh (which
    loads the same snapshot path, so the request's model id matches what it serves and
    nothing is fetched from `main`): the local server's address and a placeholder key.
    Any other model goes to ``llm_base`` if set, else to its provider's own endpoint,
    with the provider's key from the environment as litellm reads it. The placeholder is
    used only for an ``openai/`` model when ``OPENAI_API_KEY`` is unset: a keyless local
    OpenAI-compatible server (LM Studio, vLLM) still needs a non-empty key in the client,
    but a real key must never be overridden.
    """
    q = cfg.qa
    custom = os.environ.get("DOCINGEST_LLM") or q.llm
    if custom is None:
        return {
            "model": f"openai/{llm_path(cfg)}",
            "api_base": q.llm_base or DEFAULT_LLM_BASE,
            "api_key": "sk-local",  # mlx_vlm.server checks no key
            "max_tokens": 1024,
        }
    params: dict = {"model": custom, "max_tokens": 1024}
    if q.llm_base:
        params["api_base"] = q.llm_base
    if custom.startswith("openai/") and not os.environ.get("OPENAI_API_KEY"):
        params["api_key"] = "sk-local"
    return params


def local_settings(cfg: AppConfig):
    from paperqa import Settings

    _quiet_litellm()
    q = cfg.qa
    llm_cfg = {"model_list": [{"model_name": "local", "litellm_params": llm_params(cfg)}]}
    embedding = os.environ.get("DOCINGEST_EMBEDDING") or q.embedding or f"st-{embedding_path(cfg)}"
    s = Settings(
        llm="local",
        llm_config=llm_cfg,
        summary_llm="local",
        summary_llm_config=llm_cfg,
        embedding=embedding,
    )
    s.parsing.use_doc_details = False  # no network metadata lookups
    s.parsing.multimodal = False
    s.parsing.reader_config = {"chunk_chars": q.chunk_chars, "overlap": q.overlap}
    s.answer.evidence_k = q.evidence_k
    s.answer.answer_max_sources = q.answer_max_sources
    s.answer.max_concurrent_requests = q.max_concurrent_requests
    return s


def token_windows(texts, tokenizer, max_tokens: int, overlap_tokens: int = 32):
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


class PaperQAAnswerer:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def _splitter(self):
        """Token re-splitter for the pinned sentence-transformers embedder, else None."""
        if self.cfg.qa.embedding or os.environ.get("DOCINGEST_EMBEDDING"):
            return None  # custom embedder: its window is unknown here
        from transformers import AutoTokenizer

        emb_dir = Path(embedding_path(self.cfg))
        tokenizer = AutoTokenizer.from_pretrained(emb_dir)
        st_cfg = json.loads((emb_dir / "sentence_bert_config.json").read_text())
        max_tokens = st_cfg["max_seq_length"] - 2  # [CLS] and [SEP]
        return lambda texts: token_windows(texts, tokenizer, max_tokens)

    async def ask(
        self,
        question: str,
        documents: list[tuple[StoredDocument, str]],
        warn: Callable[[str], None],
    ) -> str:
        from paperqa import Docs
        from paperqa.readers import chunk_pdf
        from paperqa.types import Doc, ParsedMetadata, ParsedText

        settings = local_settings(self.cfg)
        embedding_model = settings.get_embedding_model()  # load the embedder once
        split = self._splitter()
        rc = settings.parsing.reader_config
        docs = Docs()
        for stored, markdown in documents:
            m = stored.manifest
            pages = split_pages(markdown)
            parsed = ParsedText(
                # chunk_pdf concatenates pages as-is: keep a break so words don't fuse
                content={str(n): text + "\n\n" for n, text in pages.items()},
                metadata=ParsedMetadata(
                    parsing_libraries=["docingest"],
                    total_parsed_text_length=sum(map(len, pages.values())),
                ),
            )
            partial = "" if m.complete else f", pages 1-{m.n_pages} of {m.source_pages}"
            doc = Doc(docname=m.doc_id[:16], dockey=m.doc_id, citation=m.citation() + partial)
            # Page-aware chunks: citations point at page ranges ("pages 3-4").
            texts = chunk_pdf(parsed, doc, chunk_chars=rc["chunk_chars"], overlap=rc["overlap"])
            if split:
                texts = split(texts)
            await docs.aadd_texts(texts, doc, settings=settings, embedding_model=embedding_model)
        session = await docs.aquery(question, settings=settings, embedding_model=embedding_model)
        return session.formatted_answer
