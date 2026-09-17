"""Block-range handling and bounded, self-adjusting ``eth_getLogs`` fetching."""

from __future__ import annotations

import pytest

from conflux_analytics.chain.blocks import BlockRange, parse_block, resolve_block_range
from conflux_analytics.chain.logs import LogFetcher, sort_logs
from conflux_analytics.errors import ConfigurationError, RpcError


class FakeProvider:
    """Minimal provider double that records the ranges it was asked for."""

    def __init__(self, fail_above: int | None = None, error_message: str = "block range too large"):
        self.ranges: list[tuple[int, int]] = []
        self.fail_above = fail_above
        self.error_message = error_message

    def get_logs(self, address, topics, from_block, to_block):  # noqa: ANN001, ANN201
        self.ranges.append((from_block, to_block))
        if self.fail_above is not None and to_block - from_block + 1 > self.fail_above:
            raise RpcError("eth_getLogs", -32005, self.error_message)
        return [
            {"blockNumber": hex(to_block), "logIndex": "0x0", "transactionHash": "0x" + "ab" * 32}
        ]


# ---------------------------------------------------------------------------
# BlockRange
# ---------------------------------------------------------------------------
def test_block_range_is_inclusive_and_splits_into_chunks() -> None:
    block_range = BlockRange(start=100, end=249, chunk_size=50)
    assert block_range.length == 150
    assert block_range.chunks() == [(100, 149), (150, 199), (200, 249)]


def test_block_range_chunking_tolerates_a_ragged_tail() -> None:
    assert BlockRange(start=0, end=4, chunk_size=3).chunks() == [(0, 2), (3, 4)]


def test_inverted_range_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        BlockRange(start=10, end=9, chunk_size=5)


def test_non_positive_chunk_size_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        BlockRange(start=1, end=2, chunk_size=0)


# ---------------------------------------------------------------------------
# resolve_block_range
# ---------------------------------------------------------------------------
def test_lookback_default_is_used_when_no_start_is_given() -> None:
    block_range = resolve_block_range(
        head=10_000,
        start_block=None,
        end_block=None,
        default_lookback_blocks=500,
        max_blocks_per_run=20_000,
        chunk_size=100,
    )
    assert (block_range.start, block_range.end) == (9_501, 10_000)


def test_end_block_is_clamped_to_the_head() -> None:
    block_range = resolve_block_range(
        head=1_000,
        start_block=900,
        end_block=5_000,
        default_lookback_blocks=10,
        max_blocks_per_run=100_000,
        chunk_size=100,
    )
    assert block_range.end == 1_000


def test_range_is_capped_and_keeps_the_most_recent_blocks() -> None:
    block_range = resolve_block_range(
        head=50_000,
        start_block=0,
        end_block=None,
        default_lookback_blocks=10,
        max_blocks_per_run=1_000,
        chunk_size=100,
    )
    assert block_range.length == 1_000
    assert block_range.end == 50_000
    assert block_range.start == 49_001


def test_start_after_end_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        resolve_block_range(
            head=100,
            start_block=500,
            end_block=400,
            default_lookback_blocks=10,
            max_blocks_per_run=1_000,
            chunk_size=10,
        )


# ---------------------------------------------------------------------------
# block parsing
# ---------------------------------------------------------------------------
def test_parse_block_reads_hex_fields() -> None:
    block = parse_block(
        {
            "number": "0x64",
            "hash": "0x" + "11" * 32,
            "timestamp": "0x64b8a1c0",
            "parentHash": "0x" + "22" * 32,
            "gasLimit": "0x1c9c380",
            "gasUsed": "0x5208",
            "baseFeePerGas": "0x3b9aca00",
            "transactions": [],
        }
    )
    assert block is not None
    assert block.number == 100
    assert block.timestamp == 1_689_821_632
    assert block.gas_used == 21_000
    assert block.base_fee_per_gas == 1_000_000_000


def test_parse_block_returns_none_for_missing_payload() -> None:
    assert parse_block(None) is None
    assert parse_block({}) is None


# ---------------------------------------------------------------------------
# log fetching
# ---------------------------------------------------------------------------
def test_dense_range_is_split_into_bounded_requests() -> None:
    provider = FakeProvider()
    result = LogFetcher(provider, chunk_size=100).fetch("0xpool", ["0xtopic"], 0, 249)
    assert provider.ranges == [(0, 99), (100, 199), (200, 249)]
    assert result.chunks_requested == 3
    assert result.effective_chunk_size == 100
    assert result.shrink_events == 0
    assert result.log_count == 3


def test_fetcher_halves_the_chunk_size_after_a_provider_limit() -> None:
    provider = FakeProvider(fail_above=249, error_message="query returned more than 10000 results")
    result = LogFetcher(provider, chunk_size=250, min_chunk_size=1).fetch(
        "0xpool", ["0xtopic"], 0, 249
    )
    assert result.shrink_events >= 1
    assert result.effective_chunk_size < 250
    assert result.log_count > 0
    assert result.failed_chunks == []


def test_fetcher_requires_a_positive_chunk_size() -> None:
    with pytest.raises(ValueError):
        LogFetcher(FakeProvider(), chunk_size=0)


def test_non_range_error_is_recorded_as_a_failed_chunk() -> None:
    provider = FakeProvider(error_message="execution reverted", fail_above=0)
    result = LogFetcher(provider, chunk_size=10).fetch("0xpool", ["0xtopic"], 0, 19)
    assert result.failed_chunks
    assert result.log_count == 0


def test_logs_are_sorted_by_block_then_index() -> None:
    logs = [
        {"blockNumber": "0x2", "logIndex": "0x1"},
        {"blockNumber": "0x1", "logIndex": "0x5"},
        {"blockNumber": "0x1", "logIndex": "0x2"},
    ]
    ordered = sort_logs(logs)
    assert [(log["blockNumber"], log["logIndex"]) for log in ordered] == [
        ("0x1", "0x2"),
        ("0x1", "0x5"),
        ("0x2", "0x1"),
    ]