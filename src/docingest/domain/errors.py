"""Domain errors. Adapters translate library exceptions into these."""


class DocingestError(Exception):
    """Base class for expected, reportable failures."""


class UnsupportedInputError(DocingestError, ValueError):
    """The input type is not recognized."""


class DocumentOpenError(DocingestError):
    """The document exists but cannot be opened (corrupt, encrypted, truncated)."""


class ConversionError(DocingestError):
    """A converter (LaTeX, office) could not produce text."""


class SourceUnavailableError(DocingestError):
    """A remote source (e.g. arXiv) has no downloadable content for a record."""
