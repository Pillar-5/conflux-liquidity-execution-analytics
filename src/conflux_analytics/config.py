"""Configuration system: YAML defaults, environment overrides, validation.

Priority order (highest wins):

1. Environment variables (including a local ``.env`` file).
2. The YAML file passed to ``load_settings`` (``config/default.yaml``).
3. The field defaults declared here.

DEX contract addresses are *never* hard-coded in Python: they live in
``config/dexes.yaml`` together with the source that each address was taken
from. Only addresses that verify on-chain may be used (see
:mod:`conflux_analytics.dex.registry`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ConfigurationError

DEFAULT_CONFIG_PATH = "config/default.yaml"
DEFAULT_DEX_CONFIG_PATH = "config/dexes.yaml"


def project_root() -> Path:
    """Return the directory that contains ``config/`` (search upwards)."""
    env_root = os.environ.get("CONFLUX_ANALYTICS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    for candidate in [Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[2]]:
        if (candidate / "config" / "default.yaml").is_file():
            return candidate
    return Path.cwd()


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Parse a ``.env`` file into a mapping.

    Deliberately minimal (no dependency): ``KEY=VALUE`` per line, ``#``
    comments, optional surrounding quotes. Missing files yield an empty dict.
    """
    target = path or (project_root() / ".env")
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NetworkConfig(_Model):
    """Chain and RPC endpoint configuration."""

    name: str = "mainnet"
    chain_id: int = 1030
    rpc_url: str = "https://evm.confluxrpc.com"
    ws_rpc_url: str | None = None
    explorer_url: str = "https://evm.confluxscan.org"
    explorer_api_url: str | None = None
    native_symbol: str = "CFX"
    rpc_timeout_seconds: float = 20.0
    rpc_max_retries: int = 3
    rpc_retry_backoff_seconds: float = 0.5


class DatabaseConfig(_Model):
    path: str = "data/conflux_analytics.db"


class CollectorConfig(_Model):
    log_chunk_size: int = 2000
    max_blocks_per_run: int = 20000
    max_pools: int = 25
    min_pool_reserve_raw: int = 0
    min_pool_reserve_usd: float | None = None
    checkpoints_enabled: bool = True
    default_lookback_blocks: int = 5000
    # Optional explicit block range. ``None`` means "derive from the chain head
    # and default_lookback_blocks", so a fresh clone needs no tuning.
    start_block: int | None = None
    end_block: int | None = None

    @field_validator("log_chunk_size", "max_pools")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value


class TradeSizeConfig(_Model):
    """A trade size used by the execution analytics.

    ``amount`` is expressed in whole input-token units and converted with the
    input token's on-chain decimals. ``raw_amount`` overrides it with an exact
    integer amount in the token's smallest unit.
    """

    label: str
    amount: str | None = None
    raw_amount: str | None = None

    def resolve_raw(self, decimals: int) -> int:
        """Return the raw integer amount for an input token with ``decimals``."""
        if self.raw_amount is not None:
            try:
                return int(self.raw_amount)
            except ValueError as exc:
                raise ConfigurationError(
                    f"trade size {self.label!r}: raw_amount must be an integer"
                ) from exc
        if self.amount is None:
            raise ConfigurationError(f"trade size {self.label!r}: no amount configured")
        from .chain.units import to_raw

        try:
            return to_raw(self.amount, decimals)
        except ValueError as exc:
            raise ConfigurationError(
                f"trade size {self.label!r}: amount {self.amount!r} is not a decimal number"
            ) from exc


class ExternalApiConfig(_Model):
    enabled: bool = False
    url: str | None = None


class PricingConfig(_Model):
    """Price source configuration.

    ``stablecoin_pegs`` is empty by default. Declaring a peg here is an explicit
    analytical assumption that is stored with every derived USD value as its
    ``price_source``; it is never applied implicitly.
    """

    stablecoin_pegs: dict[str, str] = Field(default_factory=dict)
    external_api: ExternalApiConfig = Field(default_factory=ExternalApiConfig)


class LoggingConfig(_Model):
    level: str = "INFO"
    format: Literal["text", "json"] = "text"
    file: str | None = None


class ApiConfig(_Model):
    host: str = "127.0.0.1"
    port: int = 8000


class DashboardConfig(_Model):
    enabled: bool = True
    mount_path: str = "/dashboard"


class TokenConfig(_Model):
    """A token the collector may discover markets for."""

    address: str
    label: str | None = None
    role: Literal["wrapped_native", "stablecoin", "other"] = "other"
    sources: list[str] = Field(default_factory=list)


class ContractConfig(_Model):
    address: str
    note: str | None = None
    sources: list[str] = Field(default_factory=list)


class DiscoveryConfig(_Model):
    method: Literal["factory", "factory_getPool", "configured"]
    enumerable: bool = False
    fee_tiers: list[int] = Field(default_factory=list)
    tick_spacing: dict[int, int] = Field(default_factory=dict)


class QuotingConfig(_Model):
    method: Literal["router_quote", "contract_simulation", "pool_math", "sdk_quote"]
    function: str | None = None


class FeeConfig(_Model):
    source: str
    bps: int | None = None
    numerator: int | None = None
    denominator: int | None = None
    tiers: list[int] = Field(default_factory=list)


class EventsConfig(_Model):
    swap: str
    mint: str | None = None
    burn: str | None = None
    pool_created: str | None = None


class DexDefinition(_Model):
    """Static, verifiable description of one DEX deployment."""

    dex_id: str
    name: str
    pool_type: Literal["constant_product", "concentrated_liquidity"]
    enabled: bool = False
    documentation: str | None = None
    contracts: dict[str, ContractConfig]
    discovery: DiscoveryConfig
    quoting: QuotingConfig
    fee: FeeConfig
    events: EventsConfig

    def contract(self, key: str) -> str | None:
        entry = self.contracts.get(key)
        return entry.address if entry else None

    def address_sources(self) -> dict[str, list[str]]:
        """Map of ``contract key -> documented sources``."""
        return {key: list(cfg.sources) for key, cfg in self.contracts.items()}


class DexRegistryConfig(_Model):
    tokens: list[TokenConfig] = Field(default_factory=list)
    dexes: list[DexDefinition] = Field(default_factory=list)

    def by_id(self, dex_id: str) -> DexDefinition:
        for dex in self.dexes:
            if dex.dex_id == dex_id:
                return dex
        raise ConfigurationError(f"unknown dex_id {dex_id!r}")


class Settings(_Model):
    """Fully resolved runtime configuration."""

    network: NetworkConfig = Field(default_factory=NetworkConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    trade_sizes: list[TradeSizeConfig] = Field(default_factory=list)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    dexes: DexRegistryConfig = Field(default_factory=DexRegistryConfig)
    report_dir: str = "reports"
    root: Path = Field(default_factory=project_root)

    @property
    def db_path(self) -> Path:
        return self._resolve(self.database.path)

    @property
    def report_path(self) -> Path:
        return self._resolve(self.report_dir)

    def _resolve(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate).resolve()

    def enabled_dexes(self) -> list[DexDefinition]:
        return [d for d in self.dexes.dexes if d.enabled]

    def tracked_token_addresses(self) -> list[str]:
        return [t.address.lower() for t in self.dexes.tokens]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"configuration file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping at the top level")
    return data


def _env(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _tristate(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_decimal_map(raw: str, key: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{key} must be a JSON object of {{address: price}}") from exc
    if not isinstance(parsed, dict):
        raise ConfigurationError(f"{key} must be a JSON object of {{address: price}}")
    return {str(k).lower(): str(v) for k, v in parsed.items()}


def _parse_trade_sizes(raw: str) -> list[dict[str, str]]:
    """Parse ``label:amount`` pairs, or ``label:raw:<integer>``."""
    entries: list[dict[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) == 2:
            entries.append({"label": parts[0].strip(), "amount": parts[1].strip()})
        elif len(parts) == 3 and parts[1].strip() == "raw":
            entries.append({"label": parts[0].strip(), "raw_amount": parts[2].strip()})
        else:
            raise ConfigurationError(
                f"TRADE_SIZES entry {chunk!r} must be 'label:amount' or 'label:raw:<integer>'"
            )
    if not entries:
        raise ConfigurationError("TRADE_SIZES must contain at least one entry")
    return entries


def apply_env_overrides(data: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Apply documented environment variables on top of YAML data."""
    network = dict(data.get("network") or {})
    mapping: dict[str, tuple[str, Any]] = {
        "CONFLUX_NETWORK": ("name", str),
        "CONFLUX_RPC_URL": ("rpc_url", str),
        "CONFLUX_WS_RPC_URL": ("ws_rpc_url", str),
        "CONFLUX_CHAIN_ID": ("chain_id", int),
        "CONFLUX_EXPLORER_URL": ("explorer_url", str),
        "CONFLUX_EXPLORER_API_URL": ("explorer_api_url", str),
        "RPC_TIMEOUT_SECONDS": ("rpc_timeout_seconds", float),
        "RPC_MAX_RETRIES": ("rpc_max_retries", int),
        "RPC_RETRY_BACKOFF_SECONDS": ("rpc_retry_backoff_seconds", float),
    }
    for var, (field, caster) in mapping.items():
        raw = _env(env, var)
        if raw is None:
            continue
        try:
            network[field] = caster(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{var} is not a valid {caster.__name__}: {raw!r}") from exc
    data["network"] = network

    collector = dict(data.get("collector") or {})
    collector_mapping: dict[str, tuple[str, Any]] = {
        "LOG_CHUNK_SIZE": ("log_chunk_size", int),
        "START_BLOCK": ("start_block", int),
        "END_BLOCK": ("end_block", int),
        "MAX_BLOCKS_PER_RUN": ("max_blocks_per_run", int),
        "MAX_POOLS": ("max_pools", int),
        "MIN_POOL_RESERVE_USD": ("min_pool_reserve_usd", float),
    }
    for var, (field, caster) in collector_mapping.items():
        raw = _env(env, var)
        if raw is None:
            continue
        try:
            collector[field] = caster(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{var} is not a valid {caster.__name__}: {raw!r}") from exc
    if _env(env, "CHECKPOINT_ENABLED") is not None:
        collector["checkpoints_enabled"] = _tristate(env["CHECKPOINT_ENABLED"])
    data["collector"] = collector

    db_path = _env(env, "CONFLUX_ANALYTICS_DB")
    if db_path:
        data["database"] = {**(data.get("database") or {}), "path": db_path}

    log_cfg = dict(data.get("logging") or {})
    if _env(env, "LOG_LEVEL"):
        log_cfg["level"] = env["LOG_LEVEL"].strip()
    if _env(env, "LOG_FORMAT"):
        log_cfg["format"] = env["LOG_FORMAT"].strip().lower()
    if _env(env, "LOG_FILE"):
        log_cfg["file"] = env["LOG_FILE"].strip()
    data["logging"] = log_cfg

    api_cfg = dict(data.get("api") or {})
    if _env(env, "API_HOST"):
        api_cfg["host"] = env["API_HOST"].strip()
    if _env(env, "API_PORT"):
        try:
            api_cfg["port"] = int(env["API_PORT"].strip())
        except ValueError as exc:
            raise ConfigurationError(
                f"API_PORT is not a valid integer: {env['API_PORT']!r}"
            ) from exc
    data["api"] = api_cfg

    if _env(env, "REPORT_DIR"):
        data["report_dir"] = env["REPORT_DIR"].strip()

    pricing = dict(data.get("pricing") or {})
    if _env(env, "PRICE_STABLECOIN_PEGS"):
        pricing["stablecoin_pegs"] = _parse_decimal_map(
            env["PRICE_STABLECOIN_PEGS"], "PRICE_STABLECOIN_PEGS"
        )
    external = dict(pricing.get("external_api") or {})
    if _env(env, "PRICE_EXTERNAL_API_ENABLED"):
        external["enabled"] = _tristate(env["PRICE_EXTERNAL_API_ENABLED"])
    if _env(env, "PRICE_EXTERNAL_API_URL"):
        external["url"] = env["PRICE_EXTERNAL_API_URL"].strip()
    pricing["external_api"] = external
    data["pricing"] = pricing

    trade_sizes = _env(env, "TRADE_SIZES")
    if trade_sizes:
        data["trade_sizes"] = _parse_trade_sizes(trade_sizes)
    return data


def load_settings(
    config_path: str | Path | None = None,
    dex_config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> Settings:
    """Load, merge and validate the complete configuration."""
    resolved_root = root or project_root()
    environment: dict[str, str] = dict(os.environ)
    environment.update(
        {k: v for k, v in load_dotenv(resolved_root / ".env").items() if k not in environment}
    )

    main_path = Path(config_path) if config_path else resolved_root / DEFAULT_CONFIG_PATH
    if not main_path.is_absolute():
        main_path = resolved_root / main_path
    data = apply_env_overrides(_load_yaml(main_path), environment)

    dex_path = Path(dex_config_path) if dex_config_path else resolved_root / DEFAULT_DEX_CONFIG_PATH
    if not dex_path.is_absolute():
        dex_path = resolved_root / dex_path
    dex_data = _load_yaml(dex_path)

    enabled_override = _env(environment, "DEXS_ENABLED")
    if enabled_override:
        wanted = {part.strip() for part in enabled_override.split(",") if part.strip()}
        declared = {entry["dex_id"] for entry in dex_data.get("dexes", [])}
        unknown = wanted - declared
        if unknown:
            raise ConfigurationError(f"DEXS_ENABLED references unknown dex ids: {sorted(unknown)}")
        for entry in dex_data.get("dexes", []):
            entry["enabled"] = entry["dex_id"] in wanted

    data["dexes"] = dex_data
    data["root"] = resolved_root
    try:
        settings = Settings.model_validate(data)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(str(exc)) from exc
    if not settings.trade_sizes:
        raise ConfigurationError("at least one trade size must be configured")
    return settings