"""Low-level EVM primitives for Conflux eSpace.

Modules in this package perform no network I/O except where explicitly noted:
they encode calldata, decode ABI return values, normalise addresses and convert
between raw integer token amounts and decimal quantities.
"""

from .units import (
    DECIMAL_PRECISION,
    decimal_str,
    from_raw,
    to_raw,
)

__all__ = ["DECIMAL_PRECISION", "decimal_str", "from_raw", "to_raw"]