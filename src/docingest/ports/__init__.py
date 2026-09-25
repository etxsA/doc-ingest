"""Ports: the interfaces the application depends on. Adapters implement them.

Every port is a ``typing.Protocol`` (structural typing), so an adapter never has to
import or subclass anything from here: any object with the right shape plugs in.
Each adapter exposes a ``fingerprint`` (name + version + settings) that feeds the
cache key, so swapping an adapter automatically invalidates stale outputs.

Images cross the ports as ``PIL.Image.Image``: the de-facto standard in-memory image
type, accepted as a pragmatic exception to "no third-party types in ports" (ADR 0001).
"""

from .converters import Conversion, DocumentConverter, Segment
from .detection import TypeDetector
from .images import ImageSource
from .ocr import OcrEngine, OcrResult
from .pdf import PdfDocument, PdfPage, PdfReader
from .qa import QuestionAnswerer
from .sources import FetchedSource, SourceCrawler, SourceRecord
from .store import DocumentStore, StoredDocument

__all__ = [
    "Conversion",
    "DocumentConverter",
    "DocumentStore",
    "FetchedSource",
    "ImageSource",
    "OcrEngine",
    "OcrResult",
    "PdfDocument",
    "PdfPage",
    "PdfReader",
    "QuestionAnswerer",
    "Segment",
    "SourceCrawler",
    "SourceRecord",
    "StoredDocument",
    "TypeDetector",
]
