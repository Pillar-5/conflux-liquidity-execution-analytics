"""Market state abstraction.

One class covers both supported market models without forcing either into the
other's shape:

* ``constant_product`` pools carry reserves.
* ``concentrated_liquidity`` pools carry ``sqrtPriceX96``, ``tick``, active
  ``liquidity``, fee tier and tick spacing.

Fields that do not apply to a model stay ``None`` and are reported as
unavailable rather than being filled with a substitute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

from ..chain.units import DECIMAL_PRECISION, from_raw, to_raw
from .common import DataStatus, PoolType, Provenance
from .token import Token

Q96 = 1 << 96


@dataclass(frozen=True)
class MarketState:
    """A point-in-time, protocol-neutral view of one pool."""

    chain_id: int
    dex_id: str
    pool_address: str
    pool_type: PoolType
    token0: Token
    token1: Token
    block_number: int
    observed_at: str
    run_id: str | None = None
    block_hash: str | None = None
    # constant product
    reserve0_raw: int | None = None
    reserve1_raw: int | None = None
    block_timestamp_last: int | None = None
    # concentrated liquidity
    sqrt_price_x96: int | None = None
    tick: int | None = None
    liquidity: int | None = None
    fee_tier_raw: int | None = None
    tick_spacing: int | None = None
    # common
    fee_bps: int | None = None
    total_supply_raw: int | None = None
    raw_state: dict[str, Any] = field(default_factory=dict)
    source: str = "rpc_eth_call"
    data_status: DataStatus = DataStatus.VERIFIED
    detail: str | None = None
    provenance: Provenance | None = None

    # -- model helpers ----------------------------------------------------
    @property
    def is_constant_product(self) -> bool:
        return self.pool_type is PoolType.CONSTANT_PRODUCT

    @property
    def is_concentrated_liquidity(self) -> bool:
        return self.pool_type is PoolType.CONCENTRATED_LIQUIDITY

    @property
    def has_decimals(self) -> bool:
        return self.token0.decimals is not None and self.token1.decimals is not None

    def reserve_raw(self, token_address: str) -> int | None:
        """Raw reserve for a pool token, or ``None`` when not applicable."""
        lowered = token_address.lower()
        if lowered == self.token0.address:
            return self.reserve0_raw
        if lowered == self.token1.address:
            return self.reserve1_raw
        return None

    def reserve_decimal(self, token_address: str) -> Decimal | None:
        """Decimal reserve for a pool token, or ``None`` when unavailable."""
        raw = self.reserve_raw(token_address)
        token = self.token0 if token_address.lower() == self.token0.address else self.token1
        if raw is None or token.decimals is None:
            return None
        return from_raw(raw, token.decimals)

    @property
    def reserves_decimal(self) -> tuple[Decimal, Decimal] | None:
        """``(reserve0, reserve1)`` in whole-token units, or ``None``."""
        if self.reserve0_raw is None or self.reserve1_raw is None or not self.has_decimals:
            return None
        assert self.token0.decimals is not None and self.token1.decimals is not None
        return (
            from_raw(self.reserve0_raw, self.token0.decimals),
            from_raw(self.reserve1_raw, self.token1.decimals),
        )

    # -- pricing ----------------------------------------------------------
    def price_token1_per_token0(self) -> Decimal | None:
        """Spot price of one token0 expressed in token1, or ``None``.

        For constant-product pools this is ``reserve1 / reserve0`` adjusted for
        decimals (the mid price, excluding fees). For concentrated-liquidity
        pools it is derived from ``sqrtPriceX96``. Both are *pool-derived*
        prices: they describe this pool's state and are not external market
        prices.
        """
        if not self.has_decimals:
            return None
        assert self.token0.decimals is not None and self.token1.decimals is not None
        decimal_shift = self.token0.decimals - self.token1.decimals
        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            if self.sqrt_price_x96 is not None:
                ratio = Decimal(self.sqrt_price_x96) / Decimal(Q96)
                return (ratio * ratio).scaleb(decimal_shift)
            if self.reserve0_raw and self.reserve1_raw:
                return (Decimal(self.reserve1_raw) / Decimal(self.reserve0_raw)).scaleb(
                    decimal_shift
                )
        return None

    def spot_price(self, token_in: str, token_out: str) -> Decimal | None:
        """Spot price of ``token_out`` per one whole ``token_in``, or ``None``."""
        base = self.price_token1_per_token0()
        if base is None:
            return None
        lowered_in = token_in.lower()
        lowered_out = token_out.lower()
        if lowered_in == self.token0.address and lowered_out == self.token1.address:
            return base
        if lowered_in == self.token1.address and lowered_out == self.token0.address:
            if base == 0:
                return None
            with localcontext() as ctx:
                ctx.prec = DECIMAL_PRECISION
                return Decimal(1) / base
        return None

    def to_raw_amount(self, token_address: str, amount: Decimal) -> int | None:
        """Convert a whole-token amount into raw units for a pool token."""
        token = self.token0 if token_address.lower() == self.token0.address else self.token1
        if token.decimals is None:
            return None
        return to_raw(amount, token.decimals)

    def as_dict(self, token_symbols: dict[str, str] | None = None) -> dict[str, Any]:
        """Flat, JSON-serialisable view used by the API and the dashboard."""
        symbols = token_symbols or {}
        price = self.price_token1_per_token0()
        return {
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "pool_type": self.pool_type.value,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "observed_at": self.observed_at,
            "run_id": self.run_id,
            "token0_address": self.token0.address,
            "token1_address": self.token1.address,
            "token0_symbol": symbols.get(self.token0.address, self.token0.symbol),
            "token1_symbol": symbols.get(self.token1.address, self.token1.symbol),
            "token0_decimals": self.token0.decimals,
            "token1_decimals": self.token1.decimals,
            "reserve0_raw": str(self.reserve0_raw) if self.reserve0_raw is not None else None,
            "reserve1_raw": str(self.reserve1_raw) if self.reserve1_raw is not None else None,
            "sqrt_price_x96": str(self.sqrt_price_x96)
            if self.sqrt_price_x96 is not None
            else None,
            "tick": self.tick,
            "liquidity": str(self.liquidity) if self.liquidity is not None else None,
            "fee_bps": self.fee_bps,
            "fee_tier_raw": self.fee_tier_raw,
            "tick_spacing": self.tick_spacing,
            "total_supply_raw": str(self.total_supply_raw)
            if self.total_supply_raw is not None
            else None,
            "price_token1_per_token0": str(price) if price is not None else None,
            "source": self.source,
            "data_status": self.data_status.value,
            "detail": self.detail,
            "raw_state": self.raw_state,
            "provenance": self.provenance.as_dict() if self.provenance else None,
        }