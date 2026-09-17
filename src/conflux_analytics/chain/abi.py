"""Minimal ABI encoding and decoding for the interfaces this project calls.

Only the subset that the supported DEX interfaces need is implemented, and each
helper is covered by unit tests. Function selectors and event topics are
computed from the canonical signature string at runtime - they are never
copy-pasted as opaque constants.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .addresses import keccak256, normalize_address

WORD_BYTES = 32
_WORD_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# Selectors and topics
# ---------------------------------------------------------------------------
def function_selector(signature: str) -> str:
    """Return the 4-byte function selector as a ``0x`` hex string."""
    return "0x" + keccak256(signature.encode("ascii")).hex()[:8]


def event_topic(signature: str) -> str:
    """Return the 32-byte event topic hash as a ``0x`` hex string."""
    return "0x" + keccak256(signature.encode("ascii")).hex()


def event_topic_bytes(signature: str) -> bytes:
    """Return the 32-byte event topic hash as raw bytes."""
    return keccak256(signature.encode("ascii"))


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------
def encode_uint(value: int) -> str:
    """Encode an unsigned integer as one 32-byte word."""
    if value < 0:
        raise ValueError(f"uint256 cannot be negative: {value}")
    if value >= 1 << 256:
        raise ValueError("uint256 overflow")
    return f"{value:064x}"


def encode_int(value: int) -> str:
    """Encode a signed integer as a two's-complement 32-byte word."""
    if not -(1 << 255) <= value < (1 << 255):
        raise ValueError("int256 out of range")
    return f"{value & ((1 << 256) - 1):064x}"


def encode_address(address: str) -> str:
    """Encode an EVM address as one 32-byte word."""
    return normalize_address(address)[2:].rjust(64, "0")


def encode_bytes3(value: bytes) -> str:
    """Encode a 3-byte value (used by Uniswap-V3 style swap paths)."""
    if len(value) != 3:
        raise ValueError("bytes3 must be exactly 3 bytes")
    return value.hex().ljust(64, "0")


def encode_bool(value: bool) -> str:
    """Encode a boolean as one 32-byte word."""
    return encode_uint(1 if value else 0)


def encode_dynamic_bytes(data: bytes) -> str:
    """Encode ``bytes`` as a length-prefixed, zero-padded dynamic argument."""
    words = (len(data) + WORD_BYTES - 1) // WORD_BYTES or 1
    padded = data.hex().ljust(WORD_BYTES * 2 * words, "0")
    return encode_uint(len(data)) + padded


def encode_uint_array(values: Iterable[int]) -> str:
    """Encode ``uint256[]`` as its tail payload (caller supplies the offset)."""
    items = list(values)
    return encode_uint(len(items)) + "".join(encode_uint(v) for v in items)


def encode_address_array(values: Sequence[str]) -> str:
    """Encode ``address[]`` as its tail payload (caller supplies the offset)."""
    return encode_uint(len(values)) + "".join(encode_address(v) for v in values)


def encode_call(signature: str, *words: str) -> str:
    """Build calldata from a selector and pre-encoded 32-byte words."""
    return function_selector(signature) + "".join(words)


# ---------------------------------------------------------------------------
# Return decoding
# ---------------------------------------------------------------------------
def strip_hex(value: str) -> str:
    """Return the hex body of a ``0x``-prefixed string."""
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"expected a 0x-prefixed hex string, got {value!r}")
    return value[2:]


def decode_words(data: str) -> list[int]:
    """Split return data into 32-byte words as unsigned integers."""
    body = strip_hex(data)
    complete = len(body) - (len(body) % (WORD_BYTES * 2))
    return [int(body[i : i + WORD_BYTES * 2], 16) for i in range(0, complete, WORD_BYTES * 2)]


def decode_uint(data: str, index: int = 0) -> int:
    """Decode the ``index``-th word as an unsigned integer."""
    return decode_words(data)[index]


def decode_int(data: str, index: int = 0) -> int:
    """Decode the ``index``-th word as a signed two's-complement integer."""
    raw = decode_words(data)[index]
    return raw - (1 << 256) if raw >= (1 << 255) else raw


def decode_address(data: str, index: int = 0) -> str:
    """Decode the ``index``-th word as an address, lower-cased."""
    body = strip_hex(data)
    word = body[index * WORD_BYTES * 2 : (index + 1) * WORD_BYTES * 2]
    if not _WORD_RE.match(word):
        raise ValueError("return data is too short to contain the requested address")
    return "0x" + word[-40:]


def _word_at(body: str, index: int) -> int:
    word = body[index * WORD_BYTES * 2 : (index + 1) * WORD_BYTES * 2]
    if not _WORD_RE.match(word):
        raise ValueError("return data is too short")
    return int(word, 16)


def decode_uint_array(data: str, offset_word_index: int = 0) -> list[int]:
    """Decode an ABI ``uint256[]`` whose offset lives at ``offset_word_index``."""
    body = strip_hex(data)
    offset = _word_at(body, offset_word_index)
    if offset * 2 + WORD_BYTES * 2 > len(body):
        raise ValueError("array offset points outside the return data")
    length = _word_at(body, offset // WORD_BYTES)
    start = offset * 2 + WORD_BYTES * 2
    end = start + length * WORD_BYTES * 2
    if end > len(body):
        raise ValueError("array length exceeds the return data")
    return [_word_at(body, offset // WORD_BYTES + 1 + i) for i in range(length)]


def decode_string(data: str) -> str:
    """Decode an ABI ``string`` return value.

    Some legacy tokens return a ``bytes32`` instead of a dynamic string; both
    shapes are handled so that metadata reading never crashes on such tokens.
    """
    body = strip_hex(data)
    if not body:
        raise ValueError("empty return data")
    if len(body) == WORD_BYTES * 2:
        raw = bytes.fromhex(body).rstrip(b"\x00")
        if raw:
            return raw.decode("utf-8", errors="replace")
        raise ValueError("empty string returned")
    offset = _word_at(body, 0)
    if offset * 2 + WORD_BYTES * 2 > len(body):
        raise ValueError("string offset points outside the return data")
    length = _word_at(body, offset // WORD_BYTES)
    start = offset * 2 + WORD_BYTES * 2
    if start + length * 2 > len(body):
        raise ValueError("string length exceeds the return data")
    return bytes.fromhex(body[start : start + length * 2]).decode("utf-8", errors="replace")