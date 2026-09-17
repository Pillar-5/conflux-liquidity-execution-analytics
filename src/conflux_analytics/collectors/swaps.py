"""Historical event collectors (swaps and liquidity changes).

All event collection is bounded: ranges come from configuration or checkpoints,
and the adapter is only asked for event types its capabilities declare.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace as dataclasses_replace
from typing import Any

from ..dex.base import DexAdapter
from ..errors import ConfluxAnalyticsError, error_category
from ..logging_setup import get_logger
from ..models.events import LiquidityEvent, SwapEvent
from ..models.pool import Pool
from ..storage.repositories import ObservationRepository

logger = get_logger(__name__)


@dataclass
class EventCollectionResult:
    """Per-dex outcome of a historical event pass."""

    dex_id: str
    from_block: int
    to_block: int
    swaps_persisted: int = 0
    liquidity_events_persisted: int = 0
    pools_attempted: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dex_id": self.dex_id,
            "from_block": self.from_block,
            "to_block": self.to_block,
            "swaps_persisted": self.swaps_persisted,
            "liquidity_events_persisted": self.liquidity_events_persisted,
            "pools_attempted": self.pools_attempted,
            "errors": list(self.errors),
            "skipped": list(self.skipped),
        }


def collect_events_for_adapter(
    adapter: DexAdapter,
    pools: list[Pool],
    observations: ObservationRepository,
    *,
    from_block: int,
    to_block: int,
    run_id: str | None = None,
    collect_swaps: bool = True,
    collect_liquidity: bool = True,
) -> EventCollectionResult:
    """Collect swap and liquidity events for every pool over a bounded range."""
    result = EventCollectionResult(dex_id=adapter.dex_id, from_block=from_block, to_block=to_block)
    for pool in pools:
        result.pools_attempted += 1
        log_extra = {
            "run_id": run_id,
            "component": "events",
            "dex": adapter.dex_id,
            "pool": pool.pool_address,
            "block_range": [from_block, to_block],
        }
        if collect_swaps:
            if not adapter.capabilities.supports_historical_events:
                result.skipped.append(
                    {"pool": pool.pool_address, "detail": "swap events unsupported"}
                )
            else:
                try:
                    swaps: list[SwapEvent] = adapter.get_swap_events(pool, from_block, to_block)
                    if run_id is not None:
                        swaps = [dataclasses_replace(s, run_id=run_id) for s in swaps]
                    result.swaps_persisted += observations.insert_swap_events(swaps)
                    logger.info(
                        "swap events collected",
                        extra={**log_extra, "count": len(swaps), "status": "ok"},
                    )
                except ConfluxAnalyticsError as exc:
                    result.errors.append(
                        {"pool": pool.pool_address, "category": error_category(exc), "detail": exc.message}
                    )
                    logger.error(
                        "swap event collection failed",
                        extra={**log_extra, "error_category": error_category(exc), "status": "error"},
                    )
        if collect_liquidity:
            if not adapter.capabilities.supports_liquidity_events:
                result.skipped.append(
                    {"pool": pool.pool_address, "detail": "liquidity events unsupported"}
                )
            else:
                try:
                    events: list[LiquidityEvent] = adapter.get_liquidity_events(pool, from_block, to_block)
                    if run_id is not None:
                        events = [dataclasses_replace(e, run_id=run_id) for e in events]
                    result.liquidity_events_persisted += observations.insert_liquidity_events(events)
                    logger.info(
                        "liquidity events collected",
                        extra={**log_extra, "count": len(events), "status": "ok"},
                    )
                except ConfluxAnalyticsError as exc:
                    result.errors.append(
                        {"pool": pool.pool_address, "category": error_category(exc), "detail": exc.message}
                    )
                    logger.error(
                        "liquidity event collection failed",
                        extra={**log_extra, "error_category": error_category(exc), "status": "error"},
                    )
    return result


__all__ = ["EventCollectionResult", "collect_events_for_adapter"]
