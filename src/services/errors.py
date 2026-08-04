"""Structured service errors used by GUI adapters."""


class ServiceError(Exception):
    """Base error with a machine-readable code and safe HTTP status."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ConfigServiceError(ServiceError):
    """Raised when the active configuration is unavailable or invalid."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 503,
        *,
        recovery_code: str | None = None,
    ) -> None:
        self.recovery_code = recovery_code or code
        super().__init__(code, message, status_code=status_code)


class NotFoundServiceError(ServiceError):
    """Raised when a requested domain object does not exist."""

    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(code, message, status_code=404)
