"""Retry policy, RPC failure classification and malformed-response handling.

All tests use synthetic failures: no network access is required, and waiting is
performed by an injected no-op sleeper so the suite stays fast.
"""

from __future__ import annotations

import json

import httpx
import pytest

from conflux_analytics.errors import (
    RpcError,
    RpcMalformedResponseError,
    RpcMethodNotSupportedError,
    RpcRateLimitError,
    RpcTimeoutError,
    RpcTransportError,
)
from conflux_analytics.rpc.provider import RpcProvider
from conflux_analytics.rpc.retry import RetryPolicy, classify, run_with_retries

CHAIN_ID = 1030


class RecordingSleeper:
    """Captures requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# ---------------------------------------------------------------------------
# policy maths
# ---------------------------------------------------------------------------
def test_backoff_is_exponential_and_bounded() -> None:
    policy = RetryPolicy(max_retries=5, backoff_seconds=0.5, max_backoff_seconds=4.0)
    assert [policy.delay_for(i) for i in range(6)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]
    assert policy.attempts() == 6


def test_negative_retry_settings_are_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_seconds=-0.1)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
def test_rate_limit_and_timeouts_are_transient() -> None:
    assert classify(RpcRateLimitError("eth_call")) == "transient"
    assert classify(RpcTimeoutError("eth_call", "timed out")) == "transient"
    assert classify(RpcTransportError("eth_call", "connection reset")) == "transient"
    assert classify(RpcMalformedResponseError("eth_call", None, "not json")) == "transient"


def test_retryable_json_rpc_codes_are_transient() -> None:
    assert classify(RpcError("eth_call", -32005, "limit exceeded")) == "transient"
    assert classify(RpcError("eth_call", -32603, "internal error")) == "transient"


def test_reverts_and_unsupported_methods_are_permanent() -> None:
    assert classify(RpcError("eth_call", 3, "execution reverted")) == "permanent"
    assert classify(RpcMethodNotSupportedError("trace_transaction", -32601, "no")) == "permanent"


# ---------------------------------------------------------------------------
# run_with_retries
# ---------------------------------------------------------------------------
def test_transient_failure_is_retried_then_succeeds() -> None:
    sleeper = RecordingSleeper()
    policy = RetryPolicy(max_retries=3, backoff_seconds=0.25, sleep=sleeper)
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RpcRateLimitError("eth_call")
        return "ok"

    assert run_with_retries(flaky, policy, "eth_call") == "ok"
    assert attempts["n"] == 3
    assert sleeper.delays == [0.25, 0.5]


def test_retries_are_bounded() -> None:
    sleeper = RecordingSleeper()
    policy = RetryPolicy(max_retries=2, backoff_seconds=0.1, sleep=sleeper)
    attempts = {"n": 0}

    def always_limited() -> None:
        attempts["n"] += 1
        raise RpcRateLimitError("eth_call")

    with pytest.raises(RpcRateLimitError):
        run_with_retries(always_limited, policy, "eth_call")
    assert attempts["n"] == 3
    assert len(sleeper.delays) == 2


def test_permanent_failure_is_not_retried() -> None:
    sleeper = RecordingSleeper()
    policy = RetryPolicy(max_retries=5, backoff_seconds=0.1, sleep=sleeper)
    attempts = {"n": 0}

    def reverts() -> None:
        attempts["n"] += 1
        raise RpcError("eth_call", 3, "execution reverted")

    with pytest.raises(RpcError):
        run_with_retries(reverts, policy, "eth_call")
    assert attempts["n"] == 1
    assert sleeper.delays == []


# ---------------------------------------------------------------------------
# provider transport behaviour
# ---------------------------------------------------------------------------
def _provider_with(handler: object) -> RpcProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.Client(transport=transport, timeout=5.0)
    return RpcProvider(
        rpc_url="https://example.invalid",
        chain_id=CHAIN_ID,
        max_retries=0,
        client=client,
    )


def test_malformed_json_body_raises_a_structured_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    provider = _provider_with(handler)
    with pytest.raises(RpcMalformedResponseError):
        provider.call("eth_chainId")


def test_json_rpc_error_object_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "reverted"}}
        )

    provider = _provider_with(handler)
    with pytest.raises(RpcError) as excinfo:
        provider.call("eth_call")
    assert excinfo.value.code == 3
    assert "reverted" in excinfo.value.rpc_message


def test_http_429_raises_a_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="too many requests")

    provider = _provider_with(handler)
    with pytest.raises(RpcRateLimitError):
        provider.call("eth_blockNumber")


def test_http_5xx_raises_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    provider = _provider_with(handler)
    with pytest.raises(RpcTransportError):
        provider.call("eth_blockNumber")


def test_missing_result_key_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})

    provider = _provider_with(handler)
    with pytest.raises(RpcMalformedResponseError):
        provider.call("eth_blockNumber")


def test_provider_counts_rpc_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = _provider_with(handler)
    assert provider.rpc_error_count == 0
    with pytest.raises(RpcTransportError):
        provider.call("eth_blockNumber")
    assert provider.rpc_error_count == 1


def test_chain_id_mismatch_is_reported_by_the_health_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(71)})

    provider = _provider_with(handler)
    assert provider.chain_id() == 71
    health = provider.health()
    assert health.chain_matches is False
    assert health.status == "mismatch"


def test_payload_is_well_formed_json_rpc() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    provider = _provider_with(handler)
    assert provider.block_number() == 1
    assert captured["method"] == "eth_blockNumber"
    assert captured["jsonrpc"] == "2.0"


def test_unsupported_trace_method_is_marked_as_not_supported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "the method trace_transaction does not exist"},
            },
        )

    provider = _provider_with(handler)
    with pytest.raises(RpcMethodNotSupportedError):
        provider.trace_transaction("0x" + "ab" * 32)