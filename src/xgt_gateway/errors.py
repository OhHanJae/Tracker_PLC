"""Project-specific exceptions."""


class GatewayError(Exception):
    """Base exception for the gateway."""


class ConfigurationError(GatewayError):
    """Raised when a configuration value is invalid."""


class XgtProtocolError(GatewayError):
    """Raised when an XGT frame is malformed or unexpected."""


class XgtPlcError(GatewayError):
    """Raised when the PLC returns an XGT NAK response."""

    def __init__(self, error_code: int, message: str | None = None) -> None:
        self.error_code = error_code
        super().__init__(message or f"PLC XGT error 0x{error_code:04X}")

