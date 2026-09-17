"""JSON-RPC provider abstraction for Conflux eSpace.

The provider is the only component that talks to the network. It:

* performs synchronous HTTP JSON-RPC calls with a configurable timeout;
* classifies failures into the structured exception hierarchy;
* retries transient failures with exponential backoff and bounded attempts;
* validates the chain ID against configuration;
* exposes a health check and a capability probe used by the analytics layer.

Nothing here assumes that an optional method (``trace_*``, ``debug_*``,
``eth_feeHistory``) exists: capabilities are probed and recorded.
"""

from __future__ import annotations

from .capabilities import (
    CAPABILITY_METHODS,
    OPTIONAL_METHODS,
    REQUIRED_METHODS,
    ProviderCapabilities,
    probe_capabilities,
)
from .provider import ProviderHealth, RpcProvider
from .retry import RetryPolicy

__all__ = [
    "CAPABILITY_METHODS",
    "OPTIONAL_METHODS",
    "REQUIRED_METHODS",
    "ProviderCapabilities",
    "ProviderHealth",
    "RetryPolicy",
    "RpcProvider",
    "probe_capabilities",
]