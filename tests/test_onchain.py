"""Тесты onchain.py — pool address extraction + cached resolver."""
from __future__ import annotations

from unittest.mock import patch

from dexbot import onchain


def test_pool_address_from_tx_extracts_inner_program_account():
    tx = {
        "events": {
            "swap": {
                "innerSwaps": [
                    {"programInfo": {"account": "POOL_ABC_123_aaaaaaaaaaaaaaaaaaaa"}},
                ],
            },
        },
    }
    assert onchain.pool_address_from_tx(tx) == "POOL_ABC_123_aaaaaaaaaaaaaaaaaaaa"


def test_pool_address_from_tx_returns_none_when_no_swap_events():
    assert onchain.pool_address_from_tx({"type": "TRANSFER"}) is None
    assert onchain.pool_address_from_tx({}) is None


def test_pool_address_from_tx_handles_malformed_payload():
    # source это название DEX, не адрес — не должны вернуть короткие строки
    assert onchain.pool_address_from_tx({"events": {"swap": {"source": "RAYDIUM"}}}) is None


def test_resolve_creation_ts_uses_l1_cache(monkeypatch):
    """Если адрес уже в _L1_CACHE — никаких сетевых вызовов."""
    onchain._L1_CACHE.clear()
    onchain._L1_CACHE["TOKEN_X"] = 1_700_000_000

    # Если DexScreener зовётся — тест провалится
    def _boom(*a, **kw):
        raise RuntimeError("должно было взяться из кэша")
    monkeypatch.setattr(onchain, "_dexscreener_pair_created_ts", _boom)
    monkeypatch.setattr(onchain, "_helius_oldest_signature_ts", _boom)

    assert onchain.resolve_creation_ts("TOKEN_X") == 1_700_000_000
    onchain._L1_CACHE.clear()


def test_resolve_creation_ts_falls_back_to_helius_when_dexscreener_misses(monkeypatch):
    onchain._L1_CACHE.clear()

    monkeypatch.setattr(onchain, "_dexscreener_pair_created_ts", lambda _: None)
    monkeypatch.setattr(onchain, "_helius_oldest_signature_ts", lambda _: 1_650_000_000)

    assert onchain.resolve_creation_ts("TOKEN_Y") == 1_650_000_000
    # Записалось в L1
    assert onchain._L1_CACHE["TOKEN_Y"] == 1_650_000_000
    onchain._L1_CACHE.clear()


def test_fetch_pool_age_min_at_clamps_to_zero(monkeypatch):
    onchain._L1_CACHE.clear()
    monkeypatch.setattr(onchain, "_dexscreener_pair_created_ts", lambda _: 1_700_000_100)
    # Signal перед creation (теоретически невозможно, но защита от bad data)
    assert onchain.fetch_pool_age_min_at("TOKEN_Z", signal_ts=1_700_000_000) == 0
    onchain._L1_CACHE.clear()


def test_fetch_pool_age_min_at_returns_minutes(monkeypatch):
    onchain._L1_CACHE.clear()
    monkeypatch.setattr(onchain, "_dexscreener_pair_created_ts", lambda _: 1_700_000_000)
    # signal 30 минут позже
    age = onchain.fetch_pool_age_min_at("TOKEN_W", signal_ts=1_700_001_800)
    assert age == 30
    onchain._L1_CACHE.clear()
