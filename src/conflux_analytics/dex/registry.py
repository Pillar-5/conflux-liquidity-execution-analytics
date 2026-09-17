"""DEX adapter registry.

Maps ``dex_id`` configuration entries to adapter classes. Adding a new DEX
means adding an adapter module and one entry here - the collectors, analytics
engine, API and dashboard are adapter-agnostic.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..errors import ConfigurationError
from ..rpc.provider import RpcProvider
from .base import DexAdapter
from .swappi_v2 import SwappiV2Adapter
from .vswap_v3 import VswapV3Adapter

ADAPTER_CLASSES: dict[str, type[DexAdapter]] = {
    "swappi_v2": SwappiV2Adapter,
    "vswap_v3": VswapV3Adapter,
}


def build_adapter(
    dex_id: str, settings: Settings, provider: RpcProvider
) -> DexAdapter:
    """Instantiate the adapter registered for ``dex_id``."""
    definition = settings.dexes.by_id(dex_id)
    adapter_cls = ADAPTER_CLASSES.get(dex_id)
    if adapter_cls is None:
        raise ConfigurationError(
            f"no adapter implementation registered for dex_id {dex_id!r}; "
            f"known: {sorted(ADAPTER_CLASSES)}"
        )
    return adapter_cls(definition, provider, settings.network.chain_id)


def build_enabled_adapters(
    settings: Settings, provider: RpcProvider, only: list[str] | None = None
) -> list[DexAdapter]:
    """Build adapters for enabled DEX definitions (optionally filtered)."""
    selected = settings.enabled_dexes()
    if only:
        wanted = {dex.lower() for dex in only}
        selected = [d for d in selected if d.dex_id.lower() in wanted]
        missing = wanted - {d.dex_id.lower() for d in selected}
        if missing:
            raise ConfigurationError(
                f"requested dex(es) not enabled in configuration: {sorted(missing)}"
            )
    return [build_adapter(d.dex_id, settings, provider) for d in selected]


def registry_info(settings: Settings) -> list[dict[str, Any]]:
    """Static identity of every configured DEX, enabled or not."""
    return [
        {
            "dex_id": d.dex_id,
            "name": d.name,
            "pool_type": d.pool_type,
            "enabled": d.enabled,
            "documentation": d.documentation,
        }
        for d in settings.dexes.dexes
    ]


__all__ = [
    "ADAPTER_CLASSES",
    "build_adapter",
    "build_enabled_adapters",
    "registry_info",
]
