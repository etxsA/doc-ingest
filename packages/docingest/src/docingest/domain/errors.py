"""Domain errors. Adapters translate library exceptions into these."""


class DocingestError(Exception):
    """Base class for expected, reportable failures."""


class UnsupportedInputError(DocingestError, ValueError):
    """The input type is not recognized."""


class DocumentOpenError(DocingestError):
    """The document exists but cannot be opened (corrupt, encrypted, truncated)."""


class ConversionError(DocingestError):
    """A converter (LaTeX, office) could not produce text."""


class InvalidQueryError(DocingestError, ValueError):
    """A crawler query that cannot be sent (empty, or an id list without ids)."""


class SourceUnavailableError(DocingestError):
    """A remote source (e.g. arXiv) has no downloadable content for a record."""


class OcrError(DocingestError):
    """The OCR engine could not transcribe a page."""


class RateLimitedError(SourceUnavailableError):
    """The source asked for a pause (429 / ``Retry-After``) that a crawler will not sit out.

    Unlike a per-record failure it concerns every later request to that source (the
    fallback URL on the same host, the next record), so :class:`CrawlService` stops the
    crawl instead of moving on. Part of the ``SourceCrawler`` contract.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s  # the pause still owed, if the server named one
