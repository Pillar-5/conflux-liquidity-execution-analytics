"""Unit tests for address handling and ConfluxScan links."""
import pytest

from conflux_analytics.chain.addresses import (
    explorer_address_url,
    explorer_block_url,
    explorer_token_url,
    explorer_tx_url,
    is_valid_address,
    normalize_address,
    shorten_address,
    to_checksum_address,
)


@pytest.mark.unit
class TestNormalize:
    def test_lowercases(self):
        assert normalize_address("0xABCDEF0123456789ABCDEF0123456789ABCDEF01") == "0xabcdef0123456789abcdef0123456789abcdef01"

    def test_idempotent(self):
        once = normalize_address("0x" + "a" * 40)
        assert normalize_address(once) == once

    def test_rejects_core_space(self):
        with pytest.raises(ValueError, match="Core Space"):
            normalize_address("cfx:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaacyfd")

    def test_rejects_short(self):
        with pytest.raises(ValueError):
            normalize_address("0x1234")

    def test_rejects_non_hex(self):
        with pytest.raises(ValueError):
            normalize_address("0x" + "z" * 40)

    def test_is_valid_address(self):
        assert is_valid_address("0x" + "0" * 40)
        assert not is_valid_address("cfx:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaacyfd")
        assert not is_valid_address(None)


@pytest.mark.unit
class TestChecksum:
    def test_known_vector(self):
        # EIP-55 test vector
        assert to_checksum_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"

    def test_all_caps_input(self):
        assert to_checksum_address("0x52908400098527886E0F7030069857D2E4169EE7") == "0x52908400098527886E0F7030069857D2E4169EE7"


@pytest.mark.unit
class TestExplorerLinks:
    EXPLORER = "https://evm.confluxscan.org"

    def test_address(self):
        assert explorer_address_url(self.EXPLORER, "0x" + "a" * 40) == f"{self.EXPLORER}/address/{'0x' + 'a' * 40}"

    def test_token(self):
        assert explorer_token_url(self.EXPLORER, "0x" + "b" * 40).endswith("/token/" + "0x" + "b" * 40)

    def test_tx_valid(self):
        url = explorer_tx_url(self.EXPLORER, "0x" + "1" * 64)
        assert url and url.endswith("/tx/" + "0x" + "1" * 64)

    def test_tx_invalid_returns_none(self):
        assert explorer_tx_url(self.EXPLORER, "not-a-hash") is None
        assert explorer_tx_url(self.EXPLORER, "") is None

    def test_block(self):
        assert explorer_block_url(self.EXPLORER, 123) == f"{self.EXPLORER}/block/123"


def test_shorten():
    assert shorten_address("0x" + "ab" * 20) == "0xababab\u2026abab"
