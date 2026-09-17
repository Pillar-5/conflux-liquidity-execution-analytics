"""DEX adapter interface.

An adapter owns all protocol-specific knowledge: where the contracts are, how
pools are discovered, how state is read, how swaps are quoted and how events
are decoded. The analytics engine and the collectors talk only to this
interface, so adding another eSpace DEX means adding an adapter, not rewriting
the pipeline.

Adapters must declare what they can do. Calling an operation an adapter does
not support raises :class:`UnsupportedCapabilityError` rather than returning a
substitute value.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..chain.tokens import TokenReader
from ..config import DexDefinition
from ..errors import UnsupportedCapabilityError
from ..logging_setup import get_logger
from ..models.common import DataStatus, PoolType
from ..models.events import LiquidityEvent, SwapEvent
from ..models.execution import FeeInfo, Quote
from ..models.market import MarketState
from ..models.pool import Pool
from ..models.token import Token
from ..rpc.provider import RpcProvider

logger = get_logger(__name__)


@dataclass(frozen=True)
class DexCapabilities:
    """What an adapter is able to do.

    The analytics layer reads these flags before requesting optional data, so
    unsupported functionality is never assumed to exist.
    """

    supports_pool_discovery: bool = False
    supports_pool_state: bool = False
    supports_quotes: bool = False
    supports_swap_simulation: bool = False
    supports_historical_events: bool = False
    supports_liquidity_events: bool = False
    supports_fee_metadata: bool = False
    supports_gas_estimation: bool = False
    supports_quoter_gas_estimate: bool = False
    supports_direct_contract_calls: bool = True
    supports_enumerable_pools: bool = False
    supports_concentrated_liquidity: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "supports_pool_discovery": self.supports_pool_discovery,
            "supports_pool_state": self.supports_pool_state,
            "supports_quotes": self.supports_quotes,
            "supports_swap_simulation": self.supports_swap_simulation,
            "supports_historical_events": self.supports_historical_events,
            "supports_liquidity_events": self.supports_liquidity_events,
            "supports_fee_metadata": self.supports_fee_metadata,
            "supports_gas_estimation": self.supports_gas_estimation,
            "supports_quoter_gas_estimate": self.supports_quoter_gas_estimate,
            "supports_direct_contract_calls": self.supports_direct_contract_calls,
            "supports_enumerable_pools": self.supports_enumerable_pools,
            "supports_concentrated_liquidity": self.supports_concentrated_liquidity,
        }


@dataclass(frozen=True)
class ContractCheck:
    """One on-chain verification step for a configured contract address."""

    contract_key: str
    address: str
    check: str
    expected: str | None
    observed: str | None
    ok: bool
    detail: str | None = None
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_key": self.contract_key,
            "address": self.address,
            "check": self.check,
            "expected": self.expected,
            "observed": self.observed,
            "ok": self.ok,
            "detail": self.detail,
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class DexHealth:
    """Health of one DEX adapter, derived from live checks."""

    dex_id: str
    status: str
    checks: list[ContractCheck] = field(default_factory=list)
    pools_checked: int = 0
    sample_pool: str | None = None
    detail: str | None = None
    checked_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "dex_id": self.dex_id,
            "status": self.status,
            "checks": [check.as_dict() for check in self.checks],
            "pools_checked": self.pools_checked,
            "sample_pool": self.sample_pool,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class DexAdapter(ABC):
    """Base class for DEX adapters."""

    def __init__(
        self,
        definition: DexDefinition,
        provider: RpcProvider,
        chain_id: int,
        token_reader: TokenReader | None = None,
    ) -> None:
        self.definition = definition
        self.provider = provider
        self.chain_id = chain_id
        self.token_reader = token_reader or TokenReader(provider, chain_id)
        self.capabilities = DexCapabilities()

    def build_gas_transaction(
        self, pool: Pool, token_in: str, token_out: str, amount_in_raw: int
    ) -> dict[str, Any] | None:
        """Build an *unsigned* swap transaction for gas estimation.

        Returns ``None`` when the DEX has no estimable transaction shape. The
        transaction is never signed or broadcast; it exists only so that
        ``eth_estimateGas`` can price the route.
        """
        return None

    # -- identity ----------------------------------------------------------
    @property
    def dex_id(self) -> str:
        return self.definition.dex_id

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def pool_type(self) -> PoolType:
        return PoolType(self.definition.pool_type)

    @property
    def documentation(self) -> str | None:
        return self.definition.documentation

    def identify(self) -> dict[str, Any]:
        """Static identity and configuration facts about this adapter."""
        return {
            "dex_id": self.dex_id,
            "name": self.name,
            "pool_type": self.pool_type.value,
            "documentation": self.documentation,
            "contracts": {
                key: {"address": cfg.address, "sources": list(cfg.sources), "note": cfg.note}
                for key, cfg in self.definition.contracts.items()
            },
            "discovery": {
                "method": self.definition.discovery.method,
                "enumerable": self.definition.discovery.enumerable,
                "fee_tiers": list(self.definition.discovery.fee_tiers),
            },
            "quoting_method": self.definition.quoting.method,
            "fee_source": self.definition.fee.source,
        }

    def get_supported_features(self) -> dict[str, Any]:
        """Capability flags, used by the API, dashboard and reports."""
        return self.capabilities.as_dict()

    # -- capability guards -------------------------------------------------
    def _require(self, capability: str) -> None:
        if not getattr(self.capabilities, capability, False):
            raise UnsupportedCapabilityError(f"{self.dex_id} does not support {capability!r}")

    # -- verification ------------------------------------------------------
    @abstractmethod
    def verify_contracts(self, block: int | str = "latest") -> list[ContractCheck]:
        """Verify configured addresses and interfaces against the chain."""

    @abstractmethod
    def health_check(self, block: int | str = "latest") -> DexHealth:
        """Return the adapter's live health, including verification results."""

    # -- discovery ---------------------------------------------------------
    @abstractmethod
    def discover_pools(
        self, tracked_tokens: Sequence[str], block: int | str = "latest"
    ) -> list[Pool]:
        """Discover pools involving the tracked tokens."""

    @abstractmethod
    def get_pool_metadata(self, pool: Pool, block: int | str = "latest") -> Pool:
        """Read pool metadata (fee tier, tick spacing) that discovery may omit."""

    # -- state -------------------------------------------------------------
    @abstractmethod
    def get_pool_state(
        self, pool: Pool, block: int | str = "latest", run_id: str | None = None
    ) -> MarketState:
        """Read the current state of a pool."""

    # -- quoting -----------------------------------------------------------
    def get_quote(
        self,
        pool: Pool,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        block: int | str = "latest",
    ) -> Quote:
        """Quote an exact-input swap. Raises when unsupported."""
        self._require("supports_quotes")
        raise UnsupportedCapabilityError(f"{self.dex_id} has no quote implementation")

    def simulate_swap(
        self,
        pool: Pool,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        block: int | str = "latest",
    ) -> Quote:
        """Simulate a swap where the protocol offers a genuine read-only path.

        Distinct from :meth:`get_quote`: only implemented when the protocol
        exposes a simulation entry point that can be executed without
        broadcasting a transaction.
        """
        self._require("supports_swap_simulation")
        raise UnsupportedCapabilityError(f"{self.dex_id} has no swap simulation")

    def get_fee_configuration(self, pool: Pool, block: int | str = "latest") -> FeeInfo:
        """Fee information for a pool."""
        self._require("supports_fee_metadata")
        raise UnsupportedCapabilityError(f"{self.dex_id} exposes no fee metadata")

    # -- events ------------------------------------------------------------
    def get_swap_events(self, pool: Pool, from_block: int, to_block: int) -> list[SwapEvent]:
        """Collect swap events for a bounded range."""
        self._require("supports_historical_events")
        raise UnsupportedCapabilityError(f"{self.dex_id} has no historical events")

    def get_liquidity_events(
        self, pool: Pool, from_block: int, to_block: int
    ) -> list[LiquidityEvent]:
        """Collect liquidity add/remove events for a bounded range."""
        self._require("supports_liquidity_events")
        raise UnsupportedCapabilityError(f"{self.dex_id} has no liquidity events")

    # -- helpers -----------------------------------------------------------
    def token_metadata(self, address: str, block: int | str = "latest") -> Token:
        """Read ERC-20 metadata through the shared reader."""
        return self.token_reader.read_metadata(address, block)

    def _empty_state(
        self,
        pool: Pool,
        block_number: int,
        detail: str,
        run_id: str | None = None,
    ) -> MarketState:
        """Build an explicitly unavailable market state.

        Used when a read fails: the record states that the data is unavailable
        instead of inventing values.
        """
        from ..models.common import utc_now_iso

        return MarketState(
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            pool_type=pool.pool_type,
            token0=self.token_metadata(pool.token0_address),
            token1=self.token_metadata(pool.token1_address),
            block_number=block_number,
            observed_at=utc_now_iso(),
            run_id=run_id,
            fee_bps=pool.fee_bps,
            fee_tier_raw=pool.fee_tier_raw,
            tick_spacing=pool.tick_spacing,
            source="rpc_eth_call",
            data_status=DataStatus.UNAVAILABLE,
            detail=detail,
        )