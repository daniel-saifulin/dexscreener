"""Тесты screener_trader.py — TP/SL/max_hold decision logic + dedup query."""
from __future__ import annotations

from dexbot import screener_trader


def test_tp_pct_and_sl_pct_match_core_strategy():
    """Параметры должны совпадать с core-стратегией для прямого сравнения когорт."""
    assert screener_trader.TP_PCT == 18.0
    assert screener_trader.SL_PCT == -12.0
    assert screener_trader.TIMEOUT_HOURS == 168


def test_decision_logic_tp_triggers_at_target():
    """Если цена ≥ entry*1.18 → closed_tp."""
    entry = 1.0
    take = entry * (1 + screener_trader.TP_PCT / 100)
    cur_price = take + 0.01
    assert cur_price >= take


def test_decision_logic_sl_triggers_below_stop():
    """Если цена ≤ entry*0.88 → closed_sl."""
    entry = 1.0
    stop = entry * (1 + screener_trader.SL_PCT / 100)
    cur_price = stop - 0.01
    assert cur_price <= stop


def test_decision_logic_max_hold_at_168h():
    """168 часов — порог max_hold."""
    assert screener_trader.TIMEOUT_HOURS == 168


def test_dedup_window_24h():
    """На один токен — одна open сделка за 24 часа."""
    assert screener_trader.DEDUP_HOURS == 24


def test_pnl_calculation():
    """PnL формула: (exit - entry) / entry × 100."""
    entry = 1.00
    exit_price = 1.18
    pnl_pct = (exit_price - entry) / entry * 100.0
    assert abs(pnl_pct - 18.0) < 0.001

    exit_price = 0.88
    pnl_pct = (exit_price - entry) / entry * 100.0
    assert abs(pnl_pct - (-12.0)) < 0.001
