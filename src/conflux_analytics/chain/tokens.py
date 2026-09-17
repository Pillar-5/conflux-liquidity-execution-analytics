"""ERC-20 metadata reader.

Reads ``name()``, ``symbol()``, ``decimals()`` and ``totalSupply()`` with
``eth_call``. Non-standard or broken contracts are tolerated: each field is
optional and a failure is recorded as ``partial`` metadata with its detail.
``decimals`` is required before any amount conversion happens - the reader
never guesses it.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..errors import RpcError
from ..logging_setup import get_logger
from ..models.common import DataStatus, Provenance
from ..models.token import Token
from ..rpc.provider import RpcProvider
from .abi import decode_string, decode_uint, strip_hex
from .addresses import normalize_address
from .units import decimal_str

logger = get_logger(__name__)

NAME_SIGNATURE = "name()"
SYMBOL_SIGNATURE = "symbol()"
DECIMALS_SIGNATURE = "decimals()"
TOTAL_SUPPLY_SIGNATURE = "totalSupply()"


class TokenReader:
    """Reads and caches ERC-20 metadata for one chain."""

    def __init__(self, provider: RpcProvider, chain_id: int) -> None:
        self.provider = provider
        self.chain_id = chain_id
        self._cache: dict[tuple[str, int | str], Token] = {}

    def read_metadata(
        self, address: str, block: int | str = "latest", use_cache: bool = True
    ) -> Token:
        """Read metadata for ``address`` at ``block``.

        Returns a :class:`Token` whose ``decimals`` is ``None`` when the
        contract does not expose it; callers must treat that as unavailable
        rather than assuming a value.
        """
        normalized = normalize_address(address)
        cache_key = (normalized, block if isinstance(block, str) else int(block))
        if use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        from ..models.common import utc_now_iso

        detail_parts: list[str] = []
        symbol = self._read_string(normalized, SYMBOL_SIGNATURE, block, detail_parts)
        name = self._read_string(normalized, NAME_SIGNATURE, block, detail_parts)
        decimals = self._read_uint(normalized, DECIMALS_SIGNATURE, block, detail_parts)
        total_supply = self._read_uint(normalized, TOTAL_SUPPLY_SIGNATURE, block, detail_parts)

        if decimals is None:
            status = DataStatus.PARTIAL
        elif symbol is None or name is None:
            status = DataStatus.PARTIAL
        elif detail_parts:
            status = DataStatus.PARTIAL
        else:
            status = DataStatus.VERIFIED

        token = Token(
            chain_id=self.chain_id,
            address=normalized,
            symbol=symbol,
            name=name,
            decimals=decimals,
            total_supply_raw=total_supply,
            metadata_status=status,
            metadata_detail="; ".join(detail_parts) if detail_parts else None,
            fetched_at=utc_now_iso(),
            provenance=Provenance(
                source="rpc_eth_call",
                method="erc20_metadata",
                chain_id=self.chain_id,
                block_number=block if isinstance(block, int) else None,
                contract_address=normalized,
                observed_at=utc_now_iso(),
                data_status=status,
                detail="; ".join(detail_parts) if detail_parts else None,
            ),
        )
        if use_cache:
            self._cache[cache_key] = token
        return token

    def _read_string(
        self, address: str, signature: str, block: int | str, detail: list[str]
    ) -> str | None:
        try:
            raw = self.provider.eth_call(address, _selector(signature), block)
        except RpcError as exc:
            detail.append(f"{signature} unavailable: {exc.rpc_message or exc}")
            return None
        if not raw or raw == "0x":
            detail.append(f"{signature} returned no data")
            return None
        try:
            return decode_string(raw)
        except ValueError as exc:
            detail.append(f"{signature} returned malformed data: {exc}")
            return None

    def _read_uint(
        self, address: str, signature: str, block: int | str, detail: list[str]
    ) -> int | None:
        try:
            raw = self.provider.eth_call(address, _selector(signature), block)
        except RpcError as exc:
            detail.append(f"{signature} unavailable: {exc.rpc_message or exc}")
            return None
        if not raw or raw == "0x":
            detail.append(f"{signature} returned no data")
            return None
        try:
            return decode_uint(raw)
        except (ValueError, IndexError) as exc:
            detail.append(f"{signature} returned malformed data: {exc}")
            return None

    def ensure_code(self, address: str, block: int | str = "latest") -> bool:
        """Return ``True`` when an address has contract code at ``block``."""
        code = self.provider.get_code(address, block)
        return bool(strip_hex(code))


def _selector(signature: str) -> str:
    from .abi import function_selector

    return function_selector(signature)


def token_to_row(token: Token) -> dict[str, Any]:
    """Convert a token into a database row."""
    row = asdict(token)
    row.pop("provenance", None)
    row["provenance_json"] = token.provenance.to_json() if token.provenance else None
    row["metadata_status"] = token.metadata_status.value
    row["total_supply_raw"] = (
        str(token.total_supply_raw) if token.total_supply_raw is not None else None
    )
    return row


def format_amount(raw: int | str | None, decimals: int | None) -> str | None:
    """Format a raw amount as a human-readable decimal string."""
    from .units import from_raw

    if raw is None or decimals is None:
        return None
    return decimal_str(from_raw(raw, decimals))