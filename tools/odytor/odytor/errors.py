"""Application-specific errors."""


class OdytorError(Exception):
    """Base error for expected odytor failures."""


class CommandError(OdytorError):
    """Raised when an external command cannot be executed."""


class FetchError(OdytorError):
    """Raised when GitHub data cannot be fetched or decoded."""


class RepoDetectionError(OdytorError):
    """Raised when a GitHub repository cannot be selected."""
