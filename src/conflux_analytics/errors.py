"""Structured exception hierarchy.

Every failure mode that the pipeline understands has a dedicated exception so
that callers can react deliberately instead of swallowing errors. Error
categories are also used as the ``error_category`` field in structured logs and
in the ``collection_runs.errors`` column.
"""

from __future__ import annotations


class ConfluxAnalyticsError(Exception):
    """Base class for all errors raised by this project."""

    category = "internal"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(ConfluxAnalyticsError):
    """Configuration is missing, malformed or internally inconsistent."""

    category = "configuration"


class VerificationError(ConfluxAnalyticsError):
    """A configured contract address or interface failed verification."""

    category = "verification"


# --------------------------------------------------------------------------
# RPC
# --------------------------------------------------------------------------
class RpcError(ConfluxAnalyticsError):
    """A JSON-RPC call returned an error object."""

    category = "rpc"

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} failed (code={code}): {message}")
        self.method = method
        self.code = code
        self.rpc_message = message


class RpcTransportError(RpcError):
    """The RPC endpoint could not be reached or returned a non-JSON response."""

    category = "rpc_transport"

    def __init__(self, method: str, message: str) -> None:
        super().__init__(method, None, message)


class RpcTimeoutError(RpcTransportError):
    """The RPC call exceeded the configured timeout."""

    category = "rpc_timeout"


class RpcRateLimitError(RpcError):
    """The provider rate-limited the request (HTTP 429)."""

    category = "rpc_rate_limit"

    def __init__(self, method: str, message: str = "rate limited") -> None:
        super().__init__(method, 429, message)


class RpcMalformedResponseError(RpcError):
    """The response was not a well-formed JSON-RPC payload."""

    category = "rpc_malformed"


class RpcMethodNotSupportedError(RpcError):
    """The provider does not expose the requested method."""

    category = "rpc_unsupported"


# --------------------------------------------------------------------------
# Capability / data availability
# --------------------------------------------------------------------------
class CapabilityUnavailableError(ConfluxAnalyticsError):
    """A DEX or provider capability that is required for an operation is off."""

    category = "capability_unavailable"


class UnsupportedCapabilityError(CapabilityUnavailableError):
    """An adapter was asked for something its capability flags forbid."""

    category = "unsupported_capability"


class DataUnavailableError(ConfluxAnalyticsError):
    """Required source data does not exist.

    Raised instead of substituting an estimate. Callers persist an
    ``unavailable`` observation rather than a fabricated value.
    """

    category = "data_unavailable"


class ChainMismatchError(ConfluxAnalyticsError):
    """The observed chain ID differs from the configured chain ID."""

    category = "chain_mismatch"

    def __init__(self, expected: int, observed: int) -> None:
        super().__init__(f"chain id mismatch: expected {expected}, observed {observed}")
        self.expected = expected
        self.observed = observed


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
class StorageError(ConfluxAnalyticsError):
    """Database access failed."""

    category = "storage"


class MigrationError(StorageError):
    """A schema migration failed or the schema version is inconsistent."""

    category = "migration"


def error_category(exc: BaseException) -> str:
    """Return the structured error category for logging and run records."""
    if isinstance(exc, ConfluxAnalyticsError):
        return exc.category
    return "unexpected"
