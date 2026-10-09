"""Composition root: the only module that knows every adapter.

``REGISTRY[port][name]`` maps the names used in ``[adapters]`` to factories. Adapter
modules are imported lazily inside the factories, so heavy optional dependencies
(mlx-vlm, docling, paperqa) load only when that adapter is selected.

Third-party adapters plug in without touching this repo, via an entry point in the
group ``docingest.<port>`` (e.g. ``docingest.ocr``) whose object is a factory
``(AppConfig) -> adapter``. Select it by its entry-point name in ``[adapters]``.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, ClassVar

from .application.ask import AskService
from .application.crawl import CrawlService
from .application.ingest import IngestService, Log
from .config import AppConfig
from .domain.models import ModelRef, SourceKind

Factory = Callable[[AppConfig], Any]


def _magic(cfg: AppConfig):
    from .adapters.detection.magic import MagicBytesDetector

    return MagicBytesDetector()


def _pdfium(cfg: AppConfig):
    from .adapters.pdf.pdfium import PdfiumReader

    return PdfiumReader()


def _ocr_profile(cfg: AppConfig):
    from .adapters.ocr.profiles import profile_for

    return profile_for(cfg.ocr.profile, cfg.ocr.prompt, cfg.ocr.max_side)


def _mlx_vlm(cfg: AppConfig):
    from .adapters.ocr.mlx_vlm import MlxVlmOcr

    o = cfg.ocr
    assert o.revision is not None
    return MlxVlmOcr(
        ModelRef(repo_id=o.repo_id, revision=o.revision),
        _ocr_profile(cfg),
        dpi=o.dpi,
        max_tokens=o.max_tokens,
        temperature=o.temperature,
        repetition_penalty=o.repetition_penalty,
    )


def _openai_ocr(cfg: AppConfig):
    from .adapters.ocr.openai_compat import OpenAICompatibleOcr

    return OpenAICompatibleOcr.from_config(cfg.ocr, _ocr_profile(cfg))


def _pillow(cfg: AppConfig):
    from .adapters.images.pillow import PillowImageSource

    return PillowImageSource()


def _docling(cfg: AppConfig):
    from .adapters.converters.docling import DoclingConverter

    return DoclingConverter()


def _pandoc(cfg: AppConfig):
    from .adapters.converters.pandoc_latex import PandocLatexConverter

    return PandocLatexConverter(cfg.latex)


def _passthrough(cfg: AppConfig):
    from .adapters.converters.plaintext import PassthroughConverter

    return PassthroughConverter()


def _filesystem(cfg: AppConfig):
    from .adapters.store.filesystem import FilesystemStore

    return FilesystemStore(Path(cfg.output_dir))


def _paperqa(cfg: AppConfig):
    from .adapters.qa.paperqa import PaperQAAnswerer

    return PaperQAAnswerer(cfg)


def _no_embedder(cfg: AppConfig):
    from .adapters.retrieval.none import NoEmbedder

    return NoEmbedder()


def _no_index(cfg: AppConfig):
    from .adapters.retrieval.none import NoIndex

    return NoIndex()


def _no_reranker(cfg: AppConfig):
    from .adapters.retrieval.none import NoReranker

    return NoReranker()


def _arxiv(cfg: AppConfig):
    from .adapters.sources.arxiv import ArxivCrawler

    return ArxivCrawler(cfg.arxiv)


REGISTRY: dict[str, dict[str, Factory]] = {
    "detector": {"magic": _magic},
    "pdf": {"pdfium": _pdfium},
    "ocr": {"mlx-vlm": _mlx_vlm, "openai-compatible": _openai_ocr},
    "images": {"pillow": _pillow},
    "office": {"docling": _docling},
    "latex": {"pandoc": _pandoc},
    "text": {"passthrough": _passthrough},
    "store": {"filesystem": _filesystem},
    "qa": {"paperqa": _paperqa},
    "crawler": {"arxiv": _arxiv},
    "embedder": {"none": _no_embedder},
    "index": {"none": _no_index},
    "reranker": {"none": _no_reranker},
}


def plugins(port: str) -> dict[str, EntryPoint]:
    """Installed entry-point plugins for a port, by name, *not* imported.

    Importing a plugin runs its module and its optional (often heavy) dependencies, so
    one broken plugin must not break a port whose selected adapter is another one: only
    the selected plugin is ever loaded, by :func:`factory`.
    """
    found: dict[str, EntryPoint] = {}
    for ep in entry_points(group=f"docingest.{port}"):
        found.setdefault(ep.name, ep)
    return found


def available(port: str) -> list[str]:
    """Names of the built-in adapters of a port plus its installed plugins (none loaded)."""
    return sorted({*REGISTRY[port], *plugins(port)})


def factory(port: str, name: str) -> Factory:
    """The factory for adapter ``name``: a built-in (which wins a name clash), else that
    one plugin, imported now. A plugin's import error keeps its type, with a note."""
    if name in REGISTRY[port]:
        return REGISTRY[port][name]
    eps = plugins(port)
    if name not in eps:
        raise ValueError(f"unknown {port} adapter {name!r}; available: {available(port)}")
    try:
        return eps[name].load()
    except Exception as e:
        e.add_note(f"while loading the {port} adapter plugin {name!r} ({eps[name].value})")
        raise


def build(port: str, cfg: AppConfig) -> Any:
    return factory(port, getattr(cfg.adapters, port))(cfg)


class Container:
    """Lazily wired object graph for one configuration."""

    def __init__(self, cfg: AppConfig, log: Log = print, overrides: dict[str, Any] | None = None):
        self.cfg = cfg
        self.log = log
        self._overrides = dict(overrides or {})  # tests / notebooks: {"ocr": FakeOcr(), ...}
        self._built: dict[str, Any] = {}

    def adapter(self, port: str) -> Any:
        if port in self._overrides:
            return self._overrides[port]
        if port not in self._built:
            self._built[port] = build(port, self.cfg)
        return self._built[port]

    @cached_property
    def ingest(self) -> IngestService:
        return IngestService(
            detector=self.adapter("detector"),
            pdf=self.adapter("pdf"),
            ocr=self.adapter("ocr"),  # cheap: models load on first transcribe()
            images=self.adapter("images"),
            converters=_LazyConverters(self),
            store=self.adapter("store"),
            policy=self.cfg.routing,
            log=self.log,
        )

    @cached_property
    def ask(self) -> AskService:
        return AskService(store=self.adapter("store"), qa=self.adapter("qa"))

    @cached_property
    def crawl(self) -> CrawlService:
        return CrawlService(
            crawler=self.adapter("crawler"),
            ingest=lambda: self.ingest,
            raw_dir=Path(self.cfg.raw_dir) / "arxiv",
            log=self.log,
        )


class _LazyConverters(dict):
    """Builds a converter adapter only when a document of that kind shows up."""

    _PORT: ClassVar[dict[SourceKind, str]] = {
        SourceKind.OFFICE: "office",
        SourceKind.LATEX: "latex",
        SourceKind.TEXT: "text",
    }

    def __init__(self, container: Container):
        super().__init__()
        self._c = container

    def __missing__(self, kind: SourceKind):
        if kind not in self._PORT:
            raise KeyError(kind)
        conv = self._c.adapter(self._PORT[kind])
        self[kind] = conv
        return conv

    def __contains__(self, kind: object) -> bool:
        return kind in self._PORT
