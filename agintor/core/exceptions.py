class AgintorError(Exception):
    """Base package error."""


class HardInvalidation(AgintorError):
    """Raised when a non-negotiable runtime invariant is violated."""


class BranchCancelled(AgintorError):
    """Raised when a branch should stop cooperatively without poisoning the whole run."""


class PromptAdaptationError(AgintorError):
    """Raised when bounded prompt adaptation cannot compile a valid runtime request."""

    def __init__(self, failure_kind: str, message: str) -> None:
        super().__init__(message)
        self.failure_kind = str(failure_kind)


class ResumeRecoveryError(HardInvalidation):
    """Raised when checkpoint recovery cannot prove a safe restart."""

    def __init__(self, failure_kind: str, message: str) -> None:
        super().__init__(message)
        self.failure_kind = str(failure_kind)


class SafetyViolation(HardInvalidation):
    """Raised when a safety boundary is crossed."""


class PatchApplyError(AgintorError):
    """Raised when a SEARCH/REPLACE patch cannot be applied exactly."""


class RuntimeLoadError(AgintorError):
    """Raised when a runtime directory or module is invalid."""


class ValidationError(AgintorError):
    """Raised when a synthesized tool or runtime does not validate."""


class ProviderError(AgintorError):
    """Base error for provider configuration and execution failures."""


class ProviderConfigurationError(ProviderError):
    """Raised when provider construction inputs are invalid."""


class ProviderExhaustedError(ProviderError):
    """Raised when an offline or bounded provider runs out of recorded responses."""
