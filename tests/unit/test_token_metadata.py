"""ERC-20 metadata reading, including non-standard and broken contracts."""

from __future__ import annotations

import pytest

from conflux_analytics.chain.abi import decode_string, encode_uint, function_selector
from conflux_analytics.chain.tokens import TokenReader, format_amount
from conflux_analytics.errors import RpcError
from conflux_analytics.models.common import DataStatus

ADDRESS = "0x" + "aa" * 20


def abi_string(value: str) -> str:
    """Encode a dynamic ``string`` return value."""
    body = value.encode("utf-8").hex()
    padded = body.ljust(((len(body) + 63) // 64) * 64, "0")
    return "0x" + encode_uint(32) + encode_uint(len(value.encode("utf-8"))) + padded


class FakeProvider:
    """Answers ``eth_call`` from a dict of selector -> response."""

    def __init__(self, responses: dict[str, str | Exception], code: str = "0x6001"):
        self.responses = responses
        self.code = code
        self.calls: list[tuple[str, str]] = []

    def eth_call(self, to: str, data: str, block: str | int = "latest") -> str:
        self.calls.append((to, data))
        selector = data[:10]
        response = self.responses.get(selector)
        if response is None:
            raise RpcError("eth_call", 3, "execution reverted")
        if isinstance(response, Exception):
            raise response
        return response

    def get_code(self, address: str, block: str | int = "latest") -> str:
        return self.code


def reader(responses: dict[str, str | Exception], **kwargs) -> TokenReader:
    return TokenReader(FakeProvider(responses, **kwargs), chain_id=1030)


SEL_NAME = function_selector("name()")
SEL_SYMBOL = function_selector("symbol()")
SEL_DECIMALS = function_selector("decimals()")
SEL_TOTAL = function_selector("totalSupply()")


def test_full_metadata_is_verified() -> None:
    token = reader(
        {
            SEL_NAME: abi_string("Wrapped CFX"),
            SEL_SYMBOL: abi_string("WCFX"),
            SEL_DECIMALS: "0x" + encode_uint(18),
            SEL_TOTAL: "0x" + encode_uint(1_000_000 * 10**18),
        }
    ).read_metadata(ADDRESS)
    assert token.symbol == "WCFX"
    assert token.name == "Wrapped CFX"
    assert token.decimals == 18
    assert token.total_supply_raw == 1_000_000 * 10**18
    assert token.metadata_status is DataStatus.VERIFIED
    assert token.provenance is not None
    assert token.provenance.source == "rpc_eth_call"


def test_symbol_is_lower_cased_for_identity_but_kept_for_display() -> None:
    token = reader({SEL_SYMBOL: abi_string("USDT"), SEL_DECIMALS: "0x" + encode_uint(6)}).read_metadata(
        ADDRESS.upper().replace("0X", "0x")
    )
    assert token.address == ADDRESS
    assert token.label == "USDT"


def test_missing_decimals_marks_metadata_partial_and_blocks_conversion() -> None:
    token = reader({SEL_SYMBOL: abi_string("ODD")}).read_metadata(ADDRESS)
    assert token.decimals is None
    assert token.metadata_status is DataStatus.PARTIAL
    assert token.has_metadata is False
    # an unknown decimal count must not silently produce a quantity
    assert token.to_decimal(1_000) is None
    assert "decimals() unavailable" in (token.metadata_detail or "")


def test_malformed_string_is_tolerated() -> None:
    token = reader(
        {SEL_SYMBOL: "0x1234", SEL_DECIMALS: "0x" + encode_uint(18)}
    ).read_metadata(ADDRESS)
    assert token.symbol is None
    assert token.decimals == 18
    assert token.metadata_status is DataStatus.PARTIAL
    assert "malformed" in (token.metadata_detail or "")


def test_bytes32_style_symbol_is_decoded() -> None:
    token = reader(
        {SEL_SYMBOL: "0x" + "4d4b52".ljust(64, "0"), SEL_DECIMALS: "0x" + encode_uint(18)}
    ).read_metadata(ADDRESS)
    assert token.symbol == "MKR"


def test_empty_return_data_is_recorded_not_guessed() -> None:
    token = reader({SEL_SYMBOL: "0x", SEL_NAME: "0x", SEL_DECIMALS: "0x"}).read_metadata(ADDRESS)
    assert token.symbol is None
    assert token.decimals is None
    assert token.metadata_status is DataStatus.PARTIAL


def test_metadata_is_cached_per_block() -> None:
    provider = FakeProvider(
        {SEL_SYMBOL: abi_string("WCFX"), SEL_DECIMALS: "0x" + encode_uint(18)}
    )
    token_reader = TokenReader(provider, chain_id=1030)
    token_reader.read_metadata(ADDRESS, 100)
    calls_after_first = len(provider.calls)
    token_reader.read_metadata(ADDRESS, 100)
    assert len(provider.calls) == calls_after_first
    token_reader.read_metadata(ADDRESS, 101)
    assert len(provider.calls) > calls_after_first


def test_ensure_code_detects_eoa_and_contract() -> None:
    assert reader({}, code="0x6001").ensure_code(ADDRESS) is True
    assert reader({}, code="0x").ensure_code(ADDRESS) is False


def test_address_normalisation_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        TokenReader(FakeProvider({}), chain_id=1030).read_metadata("not-an-address")


def test_format_amount_requires_decimals() -> None:
    assert format_amount(1_500_000_000_000_000_000, 18) == "1.500000000000000000"
    assert format_amount(1_500_000_000_000_000_000, None) is None
    assert format_amount(None, 18) is None


def test_decode_string_handles_a_plain_dynamic_string() -> None:
    assert decode_string(abi_string("hello world")) == "hello world"