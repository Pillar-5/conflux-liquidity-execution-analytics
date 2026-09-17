"""Market-state collector.

One adapter call per pool per run. The resulting :class:`MarketState` keeps the
raw on-chain quantities and the normalised view together with its block
reference, so every downstream metric is traceable to an exact block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..dex.base import DexAdapter
from ..errors import ConfluxAnalyticsError, error_category
from ..logging_setup import get_logger
from ..models.market import MarketState
from ..models.pool import Pool
from ..storage.repositories import ObservationRepository

logger = get_logger(__name__)


@dataclass
class StateCollectionResult:
    """Per-dex outcome of a market-state pass."""

    dex_id: str
    pools_attempted: int = 0
    states_collected: int = 0
    states_failed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    latency_samples_ms: list[float] = field(default_factory=list)

    @property
    def average_latency_ms(self) -> float | None:
        if not self.latency_samples_ms:
            return None
        return round(sum(self.latency_samples_ms) / len(self.latency_samples_ms), 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dex_id": self.dex_id,
            "pools_attempted": self.pools_attempted,
            "states_collected": self.states_collected,
            "states_failed": self.states_failed,
            "average_latency_ms": self.average_latency_ms,
            "errors": list(self.errors),
        }


def collect_pool_state(
    adapter: DexAdapter,
    pool: Pool,
    observations: ObservationRepository,
    *,
    block: int | str = "latest",
    run_id: str | None = None,
) -> MarketState | None:
    """Collect and persist the state of one pool.

    Returns the state, or ``None`` when the read failed (the failure is
    recorded in the result summary by the caller; nothing fabricated is stored).
    """
    import time

    started = time.perf_counter()
    try:
        state = adapter.get_pool_state(pool, block, run_id=run_id)
    except ConfluxAnalyticsError as exc:
        logger.error(
            "pool state read failed",
            extra={
                "event": "market_state.error",
                "run_id": run_id,
                "component": "market_state",
                "dex": adapter.dex_id,
                "pool": pool.pool_address,
                "error_category": error_category(exc),
                "status": "error",
            },
        )
        return None
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    observations.insert_pool_state(state)
    logger.info(
        "pool state collected",
        extra={
            "event": "market_state.collected",
            "run_id": run_id,
            "component": "market_state",
            "dex": adapter.dex_id,
            "pool": pool.pool_address,
            "block": state.block_number,
            "duration_ms": elapsed_ms,
            "status": "ok" if state.data_status.value == "verified" else state.data_status.value,
        },
    )
    return state


def collect_states_for_adapter(
    adapter: DexAdapter,
    pools: list[Pool],
    observations: ObservationRepository,
    *,
    block: int | str = "latest",
    run_id: str | None = None,
    result: StateCollectionResult | None = None,
) -> tuple[dict[str, MarketState], StateCollectionResult]:
    """Collect state for every pool of one adapter.

    Returns a mapping of ``pool_address -> MarketState`` for the states that
    could be read, plus the aggregate result.
    """
    result = result or StateCollectionResult(dex_id=adapter.dex_id)
    states: dict[str, MarketState] = {}
    import time

    for pool in pools:
        result.pools_attempted += 1
        started = time.perf_counter()
        try:
            state = adapter.get_pool_state(pool, block, run_id=run_id)
        except ConfluxAnalyticsError as exc:
            result.states_failed += 1
            result.errors.append(
                {"pool": pool.pool_address, "category": error_category(exc), "detail": exc.message}
            )
            logger.error(
                "pool state read failed",
                extra={
                    "event": "market_state.error",
                    "run_id": run_id,
                    "component": "market_state",
                    "dex": adapter.dex_id,
                    "pool": pool.pool_address,
                    "error_category": error_category(exc),
                    "status": "error",
                },
            )
            continue
        result.latency_samples_ms.append(round((time.perf_counter() - started) * 1000, 3))
        observations.insert_pool_state(state)
        states[pool.pool_address] = state
        result.states_collected += 1
        logger.info(
            "pool state collected",
            extra={
                "event": "market_state.collected",
                "run_id": run_id,
                "component": "market_state",
                "dex": adapter.dex_id,
                "pool": pool.pool_address,
                "block": state.block_number,
                "duration_ms": result.latency_samples_ms[-1],
                "status": state.data_status.value,
            },
        )
    return states, result


__all__ = [
    "StateCollectionResult",
    "collect_pool_state",
    "collect_states_for_adapter",
]
