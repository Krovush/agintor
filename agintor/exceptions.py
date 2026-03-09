class AgintorError(Exception):
    """Base package error."""


class HardInvalidation(AgintorError):
    """Raised when a non-negotiable runtime invariant is violated."""


class SafetyViolation(HardInvalidation):
    """Raised when a safety boundary is crossed."""


class PatchApplyError(AgintorError):
    """Raised when a SEARCH/REPLACE patch cannot be applied exactly."""


class RuntimeLoadError(AgintorError):
    """Raised when a runtime directory or module is invalid."""


class ValidationError(AgintorError):
    """Raised when a synthesized tool or runtime does not validate."""
