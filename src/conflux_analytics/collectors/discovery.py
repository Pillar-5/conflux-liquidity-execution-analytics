"""Pool and token discovery collector.

Discovery is adapter-driven: the collector never assumes a discovery mechanism,
it asks the adapter (whose capabilities decide) and records the discovery
source with every pool so results can be reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..chain.tokens import token_to_row
from ..dex.base import DexAdapter
from ..errors import ConfluxAnalyticsError, error_category
from ..logging_setup import get_logger
from ..models.pool import Pool
from ..models.token import Token
from ..storage.repositories import CatalogRepository

logger = get_logger(__name__)


@dataclass
class DiscoveryResult:
    """Outcome of one discovery pass."""

    dex_id: str
    pools_found: int = 0
    pools_persisted: int = 0
    tokens_persisted: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "dex_id": self.dex_id,
            "pools_found": self.pools_found,
            "pools_persisted": self.pools_persisted,
            "tokens_persisted": self.tokens_persisted,
            "errors": list(self.errors),
        }


def discover_for_adapter(
    adapter: DexAdapter,
    tracked_tokens: list[str],
    catalog: CatalogRepository,
    *,
    block: int | str = "latest",
    max_pools: int | None = None,
    persist_tokens: bool = True,
) -> DiscoveryResult:
    """Discover pools for one adapter and persist them with token metadata."""
    result = DiscoveryResult(dex_id=adapter.dex_id)
    if not adapter.capabilities.supports_pool_discovery:
        result.errors.append(
            {"category": "unsupported_capability", "detail": f"{adapter.dex_id} cannot discover pools"}
        )
        logger.warning("discovery skipped", extra={"event": "discovery.unsupported", "dex": adapter.dex_id})
        return result

    try:
        pools: list[Pool] = adapter.discover_pools(tracked_tokens, block)
    except ConfluxAnalyticsError as exc:
        result.errors.append({"category": error_category(exc), "detail": exc.message})
        logger.error(
            "discovery failed",
            extra={"event": "discovery.error", "dex": adapter.dex_id, "error_category": error_category(exc)},
        )
        return result
    if max_pools is not None:
        pools = pools[:max_pools]
    result.pools_found = len(pools)

    seen_tokens: dict[str, Token] = {}
    for pool in pools:
        try:
            catalog.upsert_pool(pool)
            result.pools_persisted += 1
        except ConfluxAnalyticsError as exc:
            result.errors.append(
                {"pool": pool.pool_address, "category": error_category(exc), "detail": exc.message}
            )
            continue
        if persist_tokens:
            for address in (pool.token0_address, pool.token1_address):
                if address in seen_tokens:
                    continue
                try:
                    token = adapter.token_metadata(address, block)
                    catalog.upsert_token(token)
                    seen_tokens[address] = token
                    result.tokens_persisted += 1
                except ConfluxAnalyticsError as exc:
                    # Metadata is optional; the pool row already records both addresses.
                    result.errors.append(
                        {"token": address, "category": error_category(exc), "detail": exc.message}
                    )
                    logger.warning(
                        "token metadata unavailable",
                        extra={"event": "discovery.token_error", "token": address, "error": str(exc)},
                    )
                    seen_tokens[address] = Token(chain_id=adapter.chain_id, address=address)

    logger.info(
        "discovery complete",
        extra={
            "event": "discovery.complete",
            "dex": adapter.dex_id,
            "pools_found": result.pools_found,
            "pools_persisted": result.pools_persisted,
        },
    )
    return result


def ensure_tokens_persisted(
    adapter: DexAdapter, pools: list[Pool], catalog: CatalogRepository, block: int | str = "latest"
) -> dict[str, Token]:
    """Persist and return token metadata for every token used by ``pools``."""
    known: dict[str, Token] = {}
    for token_row in catalog.list_tokens(chain_id=adapter.chain_id):
        known[token_row["address"].lower()] = Token(
            chain_id=token_row["chain_id"],
            address=token_row["address"],
            symbol=token_row["symbol"],
            name=token_row["name"],
            decimals=token_row["decimals"],
        )
    refreshed: dict[str, Token] = {}
    for pool in pools:
        for address in (pool.token0_address, pool.token1_address):
            if address in refreshed:
                continue
            if address in known and known[address].decimals is not None:
                refreshed[address] = known[address]
                continue
            token = adapter.token_metadata(address, block)
            catalog.upsert_token(token)
            refreshed[address] = token
    return refreshed


__all__ = ["DiscoveryResult", "discover_for_adapter", "ensure_tokens_persisted", "token_to_row"]
