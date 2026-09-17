"""Gas estimation for simulated routes.

Gas produced here is always an **estimate**: the transaction is constructed and
priced but never broadcast. Actual gas consumed by historical transactions is
collected from receipts elsewhere and is stored in a separate table; the two
quantities are never compared as if they were the same metric.

The native cost is ``gas_units * gas_price_wei`` in wei, converted to whole CFX
with 18 decimals. On Conflux eSpace the native currency is CFX with 18 decimal
places, matching the Ethereum wei convention used by the eSpace JSON-RPC.
"""

from __future__ import annotations

from decimal import localcontext

from ..chain.units import DECIMAL_PRECISION, from_raw
from ..errors import RpcError
from ..logging_setup import get_logger
from ..models.common import DataStatus
from ..models.execution import GasEstimate
from ..rpc.provider import RpcProvider

logger = get_logger(__name__)

#: Decimal places of the native currency (CFX) on Conflux eSpace.
NATIVE_DECIMALS = 18
NATIVE_SYMBOL = "CFX"

ESTIMATE_METHOD_ETH = "eth_estimateGas"
SOURCE_RPC = "rpc_eth_estimateGas"
#: Method label for a gas figure returned by the DEX's own contract.
METHOD_PROTOCOL = "protocol_contract_estimate"


def estimate_route_gas(
    provider: RpcProvider,
    transaction: dict[str, object],
    *,
    gas_price_wei: int | None = None,
    max_fee_per_gas_wei: int | None = None,
) -> GasEstimate:
    """Estimate gas for an unsigned transaction dict (never broadcast).

    ``transaction`` must be a plain dictionary of standard ``eth_estimateGas``
    fields (``from``, ``to``, ``data``, ``value``). When ``gas_price_wei`` is
    not supplied the node's suggested ``eth_gasPrice`` is used. Failures return
    a :class:`GasEstimate` with an explicit ``failure_reason`` instead of a
    fabricated number.
    """
    try:
        gas_units = provider.estimate_gas(transaction)
    except RpcError as exc:
        logger.info(
            "gas estimation unavailable",
            extra={"event": "gas.estimate_failed", "error": str(exc)},
        )
        return GasEstimate(
            method=ESTIMATE_METHOD_ETH,
            source=SOURCE_RPC,
            data_status=DataStatus.UNAVAILABLE,
            failure_reason=str(exc),
            native_symbol=NATIVE_SYMBOL,
        )

    price = gas_price_wei
    price_source = "configured"
    if price is None:
        try:
            price = provider.gas_price()
            price_source = "eth_gasPrice"
        except RpcError as exc:
            logger.info(
                "gas price unavailable",
                extra={"event": "gas.price_failed", "error": str(exc)},
            )
            return GasEstimate(
                method=ESTIMATE_METHOD_ETH,
                source=SOURCE_RPC,
                data_status=DataStatus.PARTIAL,
                gas_units=gas_units,
                failure_reason=f"gas price unavailable: {exc}",
                native_symbol=NATIVE_SYMBOL,
                detail="gas units estimated; native cost not computed without a gas price",
            )

    cost_wei = gas_units * price
    detail = f"{gas_units} units x {price} wei ({price_source})"
    return GasEstimate(
        method=ESTIMATE_METHOD_ETH,
        source=SOURCE_RPC,
        data_status=DataStatus.ESTIMATED,
        gas_units=gas_units,
        gas_price_wei=price,
        max_fee_per_gas_wei=max_fee_per_gas_wei,
        gas_cost_native_wei=cost_wei,
        native_symbol=NATIVE_SYMBOL,
        detail=detail,
    )


def cost_native_wei(gas_units: int, gas_price_wei: int) -> int:
    """Deterministic native cost in wei (kept for unit tests and reports)."""
    return gas_units * gas_price_wei


def protocol_gas_estimate(
    provider: RpcProvider,
    gas_units: int,
    *,
    source: str,
    gas_price_wei: int | None = None,
) -> GasEstimate:
    """Wrap a gas figure reported by the protocol's own contract.

    Uniswap-V3 style quoters return a ``gasEstimate`` alongside the quoted
    amount. That number comes from the DEX, not from ``eth_estimateGas``, so it
    is labelled with its own method and source and is never presented as an
    RPC estimate.

    Router-based ``eth_estimateGas`` reverts for a swap that no account can
    actually execute (no allowance, no balance), which makes the quoter's own
    figure the only available estimate for those routes.
    """
    price = gas_price_wei
    price_source = "configured"
    if price is None:
        try:
            price = provider.gas_price()
            price_source = "eth_gasPrice"
        except RpcError as exc:
            return GasEstimate(
                method=METHOD_PROTOCOL,
                source=source,
                data_status=DataStatus.PARTIAL,
                gas_units=gas_units,
                failure_reason=f"gas price unavailable: {exc}",
                native_symbol=NATIVE_SYMBOL,
                detail="protocol gas units recorded; native cost not computed",
            )
    return GasEstimate(
        method=METHOD_PROTOCOL,
        source=source,
        data_status=DataStatus.ESTIMATED,
        gas_units=gas_units,
        gas_price_wei=price,
        gas_cost_native_wei=gas_units * price,
        native_symbol=NATIVE_SYMBOL,
        detail=f"protocol-reported {gas_units} units x {price} wei ({price_source})",
    )


def wei_to_native(wei: int) -> float:
    """Whole CFX as a float, for display only. Pipeline maths stays Decimal."""
    with _prec() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return float(from_raw(wei, NATIVE_DECIMALS))


def _prec():
    """Return a decimal context manager for fixed-precision conversions."""
    return localcontext()


__all__ = [
    "ESTIMATE_METHOD_ETH",
    "METHOD_PROTOCOL",
    "NATIVE_DECIMALS",
    "NATIVE_SYMBOL",
    "SOURCE_RPC",
    "cost_native_wei",
    "estimate_route_gas",
    "protocol_gas_estimate",
    "wei_to_native",
]
