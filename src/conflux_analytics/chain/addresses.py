"""Address normalisation, EIP-55 checksums and ConfluxScan links.

Canonical identity of an asset in this project is ``(chain_id, address)`` with
the address lower-cased. Symbols are display labels only and are never used as
keys.

This project targets Conflux **eSpace**, whose addresses are ordinary 20-byte
EVM addresses rendered as ``0x`` + 40 hex characters. Core Space ``cfx:``
addresses are a different namespace and are deliberately rejected here.
"""

from __future__ import annotations

import re
from typing import Any

from Crypto.Hash import keccak

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: A basic eth_call that always succeeds: call the zero address with no data.
#: Used by the provider health check because it requires no contract state.
HEALTH_PROBE_TARGET = "0x0000000000000000000000000000000000000000"


def keccak256(data: bytes) -> bytes:
    """Return the Keccak-256 digest of ``data``."""
    hasher = keccak.new(digest_bits=256)
    hasher.update(data)
    return hasher.digest()


def normalize_address(address: str) -> str:
    """Return the canonical lower-case form of an EVM address.

    Raises :class:`ValueError` for anything that is not a 20-byte hex address,
    including Conflux Core Space ``cfx:`` addresses.
    """
    if not isinstance(address, str):
        raise ValueError(f"address must be a string, got {type(address).__name__}")
    candidate = address.strip()
    if candidate.lower().startswith("cfx:"):
        raise ValueError(
            f"{address!r} looks like a Conflux Core Space address; "
            "this project targets eSpace (0x...) addresses only"
        )
    if not candidate.startswith("0x"):
        candidate = "0x" + candidate
    if not _ADDRESS_RE.match(candidate):
        raise ValueError(f"invalid EVM address: {address!r}")
    return candidate.lower()


def is_valid_address(address: Any) -> bool:
    """Return ``True`` when ``address`` is a valid 20-byte EVM address."""
    try:
        normalize_address(address)
    except (ValueError, TypeError):
        return False
    return True


def to_checksum_address(address: str) -> str:
    """Return the EIP-55 mixed-case checksum form of an address."""
    normalized = normalize_address(address)
    body = normalized[2:]
    digest = keccak256(body.encode("ascii")).hex()
    checksummed = "".join(
        char.upper() if char.isalpha() and int(digest[index], 16) >= 8 else char
        for index, char in enumerate(body)
    )
    return "0x" + checksummed


def shorten_address(address: str, lead: int = 6, tail: int = 4) -> str:
    """Return ``0x123456…abcd`` for compact display (``lead`` hex characters)."""
    normalized = normalize_address(address)
    body = normalized[2:]
    if len(body) <= lead + tail:
        return normalized
    return f"0x{body[:lead]}…{body[-tail:]}"


def explorer_address_url(explorer_url: str, address: str) -> str:
    """ConfluxScan URL for an account or contract."""
    return f"{explorer_url.rstrip('/')}/address/{normalize_address(address)}"


def explorer_token_url(explorer_url: str, address: str) -> str:
    """ConfluxScan URL for a token."""
    return f"{explorer_url.rstrip('/')}/token/{normalize_address(address)}"


def explorer_tx_url(explorer_url: str, tx_hash: str) -> str | None:
    """ConfluxScan URL for a transaction, or ``None`` when the hash is absent."""
    if not tx_hash or not re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash):
        return None
    return f"{explorer_url.rstrip('/')}/tx/{tx_hash}"


def explorer_block_url(explorer_url: str, block_number: int) -> str:
    """ConfluxScan URL for a block."""
    return f"{explorer_url.rstrip('/')}/block/{int(block_number)}"