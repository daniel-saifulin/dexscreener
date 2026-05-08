"""Filter logic is pure — these tests don't touch network or DB."""
from __future__ import annotations

import time

from dexbot.config import Config
from dexbot.filters import evaluate_pair, evaluate_safety


def make_config(**overrides) -> Config:
    base = dict(
        database_url=None,
        chains=frozenset({"solana", "base", "ethereum"}),
        min_liquidity_usd=30_000,
        max_liquidity_usd=2_000_000,
        min_age_minutes=30,
        max_age_hours=24,
        min_volume_h1_usd=5_000,
        min_price_change_h1=-10,
        max_price_change_h1=200,
        min_buy_ratio_h1=0.55,
        max_sell_tax=5,
        max_buy_tax=5,
        max_top10_holder_pct=60,
        log_level="INFO",
    )
    base.update(overrides)
    return Config(**base)


def make_pair(*, age_minutes: float = 120, **overrides) -> dict:
    now_ms = int(time.time() * 1000)
    base = {
        "chainId": "solana",
        "pairAddress": "0xpair",
        "pairCreatedAt": now_ms - int(age_minutes * 60_000),
        "liquidity": {"usd": 50_000},
        "volume": {"h1": 10_000, "h24": 100_000},
        "priceChange": {"h1": 5, "h24": 20},
        "txns": {"h1": {"buys": 50, "sells": 30}},
        "baseToken": {"address": "TOKEN_ADDR", "symbol": "TST", "name": "Test"},
        "priceUsd": "0.001",
    }
    for k, v in overrides.items():
        base[k] = v
    return base


def test_baseline_pair_passes():
    res = evaluate_pair(make_pair(), make_config())
    assert res.passed, res.reasons


def test_low_liquidity_rejected():
    res = evaluate_pair(make_pair(liquidity={"usd": 1_000}), make_config())
    assert not res.passed
    assert any("liquidity" in r for r in res.reasons)


def test_too_young_rejected():
    res = evaluate_pair(make_pair(age_minutes=10), make_config())
    assert not res.passed
    assert any("age" in r for r in res.reasons)


def test_too_old_rejected():
    res = evaluate_pair(make_pair(age_minutes=60 * 48), make_config())
    assert not res.passed
    assert any("age" in r for r in res.reasons)


def test_low_volume_rejected():
    res = evaluate_pair(make_pair(volume={"h1": 100}), make_config())
    assert not res.passed
    assert any("vol_h1" in r for r in res.reasons)


def test_falling_knife_rejected():
    res = evaluate_pair(make_pair(priceChange={"h1": -25}), make_config())
    assert not res.passed
    assert any("chg_h1" in r for r in res.reasons)


def test_already_pumped_rejected():
    res = evaluate_pair(make_pair(priceChange={"h1": 250}), make_config())
    assert not res.passed
    assert any("chg_h1" in r for r in res.reasons)


def test_sell_dominated_rejected():
    res = evaluate_pair(
        make_pair(txns={"h1": {"buys": 20, "sells": 80}}),
        make_config(),
    )
    assert not res.passed
    assert any("buy_ratio" in r for r in res.reasons)


def test_chain_filter_excludes():
    res = evaluate_pair(make_pair(chainId="bsc"), make_config())
    assert not res.passed
    assert any("chain" in r for r in res.reasons)


def test_tiny_sample_skips_buy_ratio_check():
    # With <10 transactions, buy ratio is statistically meaningless and is skipped.
    res = evaluate_pair(
        make_pair(txns={"h1": {"buys": 1, "sells": 5}}),
        make_config(),
    )
    assert res.passed, res.reasons


def test_safety_honeypot_rejected():
    reasons = evaluate_safety({"is_honeypot": True, "flags": []}, make_config())
    assert "honeypot" in reasons


def test_safety_high_sell_tax_rejected():
    reasons = evaluate_safety({"is_honeypot": False, "sell_tax": 25.0, "flags": []}, make_config())
    assert any("sell_tax" in r for r in reasons)


def test_safety_concentrated_holders_rejected():
    reasons = evaluate_safety({"top10_holder_pct": 85.0, "flags": []}, make_config())
    assert any("top10_holder" in r for r in reasons)


def test_safety_clean_passes():
    reasons = evaluate_safety({
        "is_honeypot": False, "sell_tax": 1.0, "buy_tax": 1.0,
        "top10_holder_pct": 30.0, "flags": [],
    }, make_config())
    assert reasons == []


def test_safety_dangerous_flag_rejected():
    reasons = evaluate_safety({"flags": ["can_take_back_ownership"]}, make_config())
    assert any("can_take_back_ownership" in r for r in reasons)
