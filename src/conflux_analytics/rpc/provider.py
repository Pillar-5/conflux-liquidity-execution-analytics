"""Synchronous HTTP JSON-RPC provider for Conflux eSpace."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import httpx

from ..errors import (
    ChainMismatchError,
    RpcError,
    RpcMalformedResponseError,
    RpcMethodNotSupportedError,
    RpcRateLimitError,
    RpcTimeoutError,
    RpcTransportError,
)
from ..logging_setup import get_logger
from .retry import (
    METHOD_NOT_FOUND_CODE,
    NOT_SUPPORTED_MESSAGE_MARKERS,
    RetryPolicy,
    run_with_retries,
)

logger = get_logger(__name__)

#: HTTP status codes that are retried rather than surfaced as permanent.
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a provider health check."""

    rpc_url: str
    reachable: bool
    status: str
    latency_ms: float | None
    chain_id_observed: int | None
    chain_id_expected: int
    chain_matches: bool
    latest_block: int | None
    eth_call_ok: bool
    detail: str | None = None
    checked_at: str = field(default="")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rpc_url": self.rpc_url,
            "reachable": self.reachable,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "chain_id_observed": self.chain_id_observed,
            "chain_id_expected": self.chain_id_expected,
            "chain_matches": self.chain_matches,
            "latest_block": self.latest_block,
            "eth_call_ok": self.eth_call_ok,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class RpcProvider:
    """JSON-RPC client with retries, timeouts and chain validation.

    The provider owns a single :class:`httpx.Client` reused across a run.
    Close it with :meth:`close` or use the provider as a context manager.
    """

    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.rpc_url = rpc_url
        self.expected_chain_id = chain_id
        self.timeout_seconds = timeout_seconds
        self.policy = RetryPolicy(
            max_retries=max_retries,
            backoff_seconds=retry_backoff_seconds,
            sleep=sleep,
        )
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        self._request_id = 0
        self._rpc_error_count = 0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the underlying HTTP client when this provider owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RpcProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def rpc_error_count(self) -> int:
        """Number of RPC calls that failed (reported by the benchmark)."""
        return self._rpc_error_count

    # -- transport ---------------------------------------------------------
    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = str(payload.get("method", "?"))
        try:
            response = self._client.post(
                self.rpc_url,
                json=payload,
                headers={"content-type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise RpcTimeoutError(method, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise RpcTransportError(method, str(exc)) from exc

        if response.status_code in RETRYABLE_HTTP_STATUS:
            if response.status_code == 429:
                raise RpcRateLimitError(method, response.text[:200] or "HTTP 429")
            raise RpcTransportError(method, f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise RpcTransportError(method, f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            parsed = response.json()
        except json.JSONDecodeError as exc:
            raise RpcMalformedResponseError(method, None, "response body is not JSON") from exc
        if not isinstance(parsed, dict):
            raise RpcMalformedResponseError(method, None, "response is not a JSON object")
        return parsed

    def call(self, method: str, params: Sequence[Any] | None = None) -> Any:
        """Perform an RPC call and return its ``result``."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": list(params or []),
        }

        def _invoke() -> Any:
            parsed = self._post(payload)
            error = parsed.get("error")
            if error is not None:
                code = error.get("code")
                message = str(error.get("message", ""))
                if code == METHOD_NOT_FOUND_CODE or any(
                    marker in message.lower() for marker in NOT_SUPPORTED_MESSAGE_MARKERS
                ):
                    raise RpcMethodNotSupportedError(method, code, message)
                if code in (429, -32005):
                    raise RpcRateLimitError(method, message)
                raise RpcError(method, code, message)
            if "result" not in parsed:
                raise RpcMalformedResponseError(method, None, "response has no result field")
            return parsed["result"]

        try:
            return run_with_retries(_invoke, self.policy, method)
        except Exception:
            self._rpc_error_count += 1
            raise

    def call_raw(self, method: str, params: Sequence[Any] | None = None) -> dict[str, Any]:
        """Perform an RPC call and return the whole response envelope.

        Used by capability probing and raw observation capture, where the error
        object itself is the information we want to keep.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": list(params or []),
        }
        try:
            return self._post(payload)
        except RpcError as exc:
            return {"error": {"code": exc.code, "message": exc.rpc_message or str(exc)}}
        except Exception as exc:  # noqa: BLE001 - captured as data, not swallowed
            return {"error": {"code": None, "message": str(exc)}}

    # -- curated read methods ---------------------------------------------
    def chain_id(self) -> int:
        """Return the node's chain ID as an integer."""
        return int(self.call("eth_chainId"), 16)

    def assert_chain_id(self) -> int:
        """Validate the node's chain ID against configuration."""
        observed = self.chain_id()
        if observed != self.expected_chain_id:
            raise ChainMismatchError(self.expected_chain_id, observed)
        return observed

    def block_number(self) -> int:
        """Return the latest block number."""
        return int(self.call("eth_blockNumber"), 16)

    def get_block_by_number(
        self, block: str | int, full_transactions: bool = False
    ) -> dict[str, Any] | None:
        """Fetch a block by number or tag."""
        tag = block if isinstance(block, str) else hex(block)
        return self.call("eth_getBlockByNumber", [tag, full_transactions])

    def get_block_by_hash(
        self, block_hash: str, full_transactions: bool = False
    ) -> dict[str, Any] | None:
        """Fetch a block by hash."""
        return self.call("eth_getBlockByHash", [block_hash, full_transactions])

    def get_transaction_by_hash(self, tx_hash: str) -> dict[str, Any] | None:
        """Fetch a transaction by hash."""
        return self.call("eth_getTransactionByHash", [tx_hash])

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """Fetch a transaction receipt by hash."""
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def get_code(self, address: str, block: str | int = "latest") -> str:
        """Fetch contract code (``0x`` for an externally owned account)."""
        tag = block if isinstance(block, str) else hex(block)
        return self.call("eth_getCode", [address, tag])

    def get_balance(self, address: str, block: str | int = "latest") -> int:
        """Fetch an account balance in wei."""
        tag = block if isinstance(block, str) else hex(block)
        return int(self.call("eth_getBalance", [address, tag]), 16)

    def get_storage_at(self, address: str, slot: str | int, block: str | int = "latest") -> str:
        """Read a storage slot."""
        tag = block if isinstance(block, str) else hex(block)
        slot_hex = slot if isinstance(slot, str) else hex(slot)
        return self.call("eth_getStorageAt", [address, slot_hex, tag])

    def eth_call(
        self,
        to: str,
        data: str,
        block: str | int = "latest",
        state_overrides: dict[str, Any] | None = None,
        from_address: str | None = None,
    ) -> str:
        """Execute a read-only contract call and return its return data."""
        tag = block if isinstance(block, str) else hex(block)
        request: dict[str, Any] = {"to": to, "data": data}
        if from_address:
            request["from"] = from_address
        params: list[Any] = [request, tag]
        if state_overrides:
            params.append(state_overrides)
        return self.call("eth_call", params)

    def estimate_gas(
        self,
        transaction: dict[str, Any],
        block: str | int = "latest",
        state_overrides: dict[str, Any] | None = None,
    ) -> int:
        """Estimate gas for an unsigned transaction. Never broadcasts."""
        tag = block if isinstance(block, str) else hex(block)
        params: list[Any] = [transaction, tag]
        if state_overrides:
            params.append(state_overrides)
        return int(self.call("eth_estimateGas", params), 16)

    def get_logs(
        self,
        address: str | Sequence[str] | None = None,
        topics: Sequence[Any] | None = None,
        from_block: int | str = "latest",
        to_block: int | str = "latest",
    ) -> list[dict[str, Any]]:
        """Fetch logs for a bounded block range."""
        filter_params: dict[str, Any] = {
            "fromBlock": from_block if isinstance(from_block, str) else hex(from_block),
            "toBlock": to_block if isinstance(to_block, str) else hex(to_block),
        }
        if address is not None:
            filter_params["address"] = address
        if topics:
            filter_params["topics"] = list(topics)
        result = self.call("eth_getLogs", [filter_params])
        if not isinstance(result, list):
            raise RpcMalformedResponseError("eth_getLogs", None, "expected a list of logs")
        return result

    # -- gas and fee data --------------------------------------------------
    def gas_price(self) -> int:
        """Return the node's suggested gas price in wei."""
        return int(self.call("eth_gasPrice"), 16)

    def max_priority_fee_per_gas(self) -> int | None:
        """Return the suggested priority fee, or ``None`` when unsupported."""
        try:
            return int(self.call("eth_maxPriorityFeePerGas"), 16)
        except RpcError:
            return None

    def fee_history(
        self, block_count: int, newest_block: str | int, percentiles: Sequence[int]
    ) -> dict[str, Any] | None:
        """Fetch fee history, or ``None`` when the provider does not support it."""
        tag = newest_block if isinstance(newest_block, str) else hex(newest_block)
        try:
            return self.call("eth_feeHistory", [hex(block_count), tag, list(percentiles)])
        except RpcError:
            return None

    # -- optional tracing --------------------------------------------------
    def trace_transaction(self, tx_hash: str) -> Any:
        """Fetch an OpenEthereum/Parity-style trace.

        Only call after :func:`probe_capabilities` reports support.
        """
        return self.call("trace_transaction", [tx_hash])

    def debug_trace_transaction(self, tx_hash: str) -> Any:
        """Fetch a Geth-style trace. Usually disabled on public endpoints."""
        return self.call("debug_traceTransaction", [tx_hash, {}])

    # -- health ------------------------------------------------------------
    def health(self) -> ProviderHealth:
        """Check reachability, chain ID, block height and a basic eth_call."""
        from datetime import datetime

        checked_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        started = time.perf_counter()
        try:
            observed_chain_id = self.chain_id()
            latest_block = self.block_number()
        except RpcError as exc:
            return ProviderHealth(
                rpc_url=self.rpc_url,
                reachable=False,
                status="unreachable",
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                chain_id_observed=None,
                chain_id_expected=self.expected_chain_id,
                chain_matches=False,
                latest_block=None,
                eth_call_ok=False,
                detail=str(exc),
                checked_at=checked_at,
            )

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        eth_call_ok = False
        detail: str | None = None
        try:
            self.eth_call("0x0000000000000000000000000000000000000000", "0x", "latest")
            eth_call_ok = True
        except RpcError as exc:
            detail = f"eth_call probe failed: {exc}"

        chain_matches = observed_chain_id == self.expected_chain_id
        if not chain_matches:
            detail = (
                f"chain id mismatch: configured {self.expected_chain_id}, "
                f"node reports {observed_chain_id}"
            )
        status = "ok" if (chain_matches and eth_call_ok) else (
            "degraded" if chain_matches else "mismatch"
        )
        return ProviderHealth(
            rpc_url=self.rpc_url,
            reachable=True,
            status=status,
            latency_ms=latency_ms,
            chain_id_observed=observed_chain_id,
            chain_id_expected=self.expected_chain_id,
            chain_matches=chain_matches,
            latest_block=latest_block,
            eth_call_ok=eth_call_ok,
            detail=detail,
            checked_at=checked_at,
        )