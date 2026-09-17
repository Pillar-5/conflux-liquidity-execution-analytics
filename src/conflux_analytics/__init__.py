"""Conflux Liquidity & Execution Analytics.

Deterministic blockchain analytics infrastructure for Conflux eSpace DEX
markets: collection, normalisation, liquidity measurement and execution-quality
analytics.

The package is a modular monolith. Sub-packages:

``rpc``          JSON-RPC provider abstraction, retries, capability probing.
``chain``        Low-level EVM primitives (addresses, ABI encoding, blocks, logs).
``dex``          DEX adapters behind a common capability-aware interface.
``collectors``   Discovery, market state, event and checkpoint collection.
``analytics``    Liquidity, execution, pricing, fee and gas calculations.
``storage``      SQLite schema, migrations and repositories.
``api``          FastAPI application and dashboard.
``reporting``    Reproducible CSV/JSON/Markdown reports.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
