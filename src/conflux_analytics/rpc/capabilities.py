"""Provider capability probing.

Public Conflux eSpace endpoints expose the standard EVM method set but not
necessarily the optional ones (``trace_*``, ``debug_*``, ``eth_feeHistory``)
nor Geth state overrides. Rather than assuming, each method is probed once and
the outcome is recorded - including the exact error the node returned - so the
analytics layer can label dependent results as ``unavailable`` instead of
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..logging_setup import get_logger
from .provider import RpcProvider

logger = get_logger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

#: Methods the pipeline cannot work without.
REQUIRED_METHODS: tuple[str, ...] = (
    "eth_chainId",
    "eth_blockNumber",
    "eth_getBlockByNumber",
    "eth_call",
    "eth_getCode",
    "eth_getLogs",
)

#: Methods that unlock optional analysis. Absence degrades a feature, not the run.
OPTIONAL_METHODS: tuple[str, ...] = (
    "eth_getBlockByHash",
    "eth_getBalance",
    "eth_getStorageAt",
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
    "eth_estimateGas",
    "eth_gasPrice",
    "eth_maxPriorityFeePerGas",
    "eth_feeHistory",
    "net_version",
    "eth_syncing",
    "trace_transaction",
    "debug_traceTransaction",
)

CAPABILITY_METHODS: tuple[str, ...] = REQUIRED_METHODS + OPTIONAL_METHODS


@dataclass(frozen=True)
class MethodProbe:
    """Outcome of probing one RPC method."""

    method: str
    supported: bool
    error_code: int | None = None
    error_message: str | None = None
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "supported": self.supported,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "required": self.required,
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    """Recorded capability set for one RPC endpoint."""

    rpc_url: str
    checked_at: str
    probes: dict[str, MethodProbe] = field(default_factory=dict)
    block_number: int | None = None
    state_overrides_supported: bool = False
    state_overrides_detail: str | None = None

    def supports(self, method: str) -> bool:
        probe = self.probes.get(method)
        return bool(probe and probe.supported)

    @property
    def required_ok(self) -> bool:
        return all(probe.supported for probe in self.probes.values() if probe.required)

    @property
    def missing_required(self) -> list[str]:
        return sorted(
            probe.method
            for probe in self.probes.values()
            if probe.required and not probe.supported
        )

    @property
    def supports_trace_transaction(self) -> bool:
        return self.supports("trace_transaction")

    @property
    def supports_debug_trace_transaction(self) -> bool:
        return self.supports("debug_traceTransaction")

    @property
    def supports_estimate_gas(self) -> bool:
        return self.supports("eth_estimateGas")

    @property
    def supports_fee_history(self) -> bool:
        return self.supports("eth_feeHistory")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rpc_url": self.rpc_url,
            "checked_at": self.checked_at,
            "block_number": self.block_number,
            "required_ok": self.required_ok,
            "missing_required": self.missing_required,
            "state_overrides_supported": self.state_overrides_supported,
            "state_overrides_detail": self.state_overrides_detail,
            "methods": {name: probe.as_dict() for name, probe in self.probes.items()},
        }

    def rows(self) -> list[dict[str, Any]]:
        """Flatten to one row per method for persistence."""
        return [
            {"rpc_url": self.rpc_url, "checked_at": self.checked_at, **probe.as_dict()}
            for probe in self.probes.values()
        ]


def _probe_params(
    method: str, block_number: int, block_hash: str | None, sample_tx: str | None
) -> list[Any] | None:
    """Return probe parameters, or ``None`` when a required input is missing."""
    low_block = max(block_number - 2, 0)
    if method in {"eth_chainId", "eth_blockNumber", "eth_gasPrice", "eth_syncing", "net_version",
                  "eth_maxPriorityFeePerGas"}:
        return []
    if method == "eth_getBlockByNumber":
        return [hex(low_block), False]
    if method == "eth_getBlockByHash":
        return [block_hash, False] if block_hash else None
    if method in {"eth_getTransactionByHash", "eth_getTransactionReceipt", "trace_transaction"}:
        return [sample_tx] if sample_tx else None
    if method == "debug_traceTransaction":
        return [sample_tx, {}] if sample_tx else None
    if method == "eth_call":
        return [{"to": ZERO_ADDRESS, "data": "0x"}, "latest"]
    if method in {"eth_getCode", "eth_getBalance"}:
        return [ZERO_ADDRESS, "latest"]
    if method == "eth_getStorageAt":
        return [ZERO_ADDRESS, "0x0", "latest"]
    if method == "eth_getLogs":
        return [{"fromBlock": hex(low_block), "toBlock": hex(block_number)}]
    if method == "eth_estimateGas":
        return [{"from": ZERO_ADDRESS, "to": ZERO_ADDRESS, "value": "0x1"}, "latest"]
    if method == "eth_feeHistory":
        return ["0x2", hex(block_number), [25, 50, 75]]
    return []


def probe_capabilities(
    provider: RpcProvider,
    block_number: int | None = None,
    block_hash: str | None = None,
    sample_tx_hash: str | None = None,
) -> ProviderCapabilities:
    """Probe every method of interest and record the outcome.

    ``sample_tx_hash`` should be supplied when a recent transaction is known;
    the tracing probes are otherwise recorded as "not probed" rather than being
    reported as unsupported.
    """
    checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = block_number if block_number is not None else provider.block_number()
    probes: dict[str, MethodProbe] = {}

    for method in CAPABILITY_METHODS:
        params = _probe_params(method, head, block_hash, sample_tx_hash)
        required = method in REQUIRED_METHODS
        if params is None:
            probes[method] = MethodProbe(
                method=method,
                supported=False,
                error_message="not probed: no sample transaction available",
                required=required,
            )
            continue
        response = provider.call_raw(method, params)
        if "result" in response and response["result"] is not None:
            probes[method] = MethodProbe(method=method, supported=True, required=required)
        else:
            error = response.get("error") or {}
            probes[method] = MethodProbe(
                method=method,
                supported=False,
                error_code=error.get("code"),
                error_message=str(error.get("message", "unknown error")),
                required=required,
            )

    override_params: list[Any] = [
        {"to": ZERO_ADDRESS, "data": "0x"},
        "latest",
        {ZERO_ADDRESS: {"balance": "0x0"}},
    ]
    response = provider.call_raw("eth_call", override_params)
    overrides_supported = "result" in response and "error" not in response
    overrides_detail = None
    if not overrides_supported:
        error = response.get("error") or {}
        overrides_detail = str(error.get("message", "state overrides rejected"))

    capabilities = ProviderCapabilities(
        rpc_url=provider.rpc_url,
        checked_at=checked_at,
        probes=probes,
        block_number=head,
        state_overrides_supported=overrides_supported,
        state_overrides_detail=overrides_detail,
    )
    if not capabilities.required_ok:
        logger.warning(
            "provider is missing required RPC methods",
            extra={
                "event": "provider.capabilities",
                "rpc_url": provider.rpc_url,
                "missing_required": capabilities.missing_required,
            },
        )
    return capabilities