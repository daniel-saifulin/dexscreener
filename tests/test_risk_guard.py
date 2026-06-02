"""Тесты risk_guard — критическая защита live-торговли. Покрываем все ветки."""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from dexbot import risk_guard
from dexbot.risk_guard import CapitalSnapshot, check_can_open


def _conn_with_state(snap: CapitalSnapshot, *, open_positions: int = 0,
                     trades_today: int = 0):
    """Мок connection возвращающий заданное состояние."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = None

    call_sequence = [
        (snap.wallet_balance_usd, snap.peak_balance_usd, snap.daily_pnl_usd,
         snap.daily_anchor_date, snap.is_halted, snap.halt_reason),
        (open_positions,),
        (trades_today,),
    ]
    cursor.fetchone.side_effect = call_sequence
    return conn


def _enable_live(monkeypatch):
    monkeypatch.setattr(risk_guard, "LIVE_TRADING_ENABLED", True)


def test_kill_switch_blocks_everything(monkeypatch):
    monkeypatch.setattr(risk_guard, "LIVE_TRADING_ENABLED", False)
    conn = MagicMock()
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "LIVE_TRADING_ENABLED" in r.reason


def test_halted_state_blocks(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), True, "test halt")
    conn = _conn_with_state(snap)
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "halted" in r.reason


def test_position_size_over_limit_blocked(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap)
    r = check_can_open(conn, proposed_position_usd=100.0)  # > MAX_POSITION_USD=5
    assert not r.allowed
    assert "position" in r.reason and "limit" in r.reason


def test_insufficient_balance_blocked(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(2.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap)
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "insufficient" in r.reason


def test_daily_loss_limit_triggers_halt(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(40.0, 50.0, -10.0, datetime.date.today(), False, None)
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = None
    # Возвращаем состояние при первом запросе, потом ещё раз при set_halt
    cursor.fetchone.side_effect = [
        (40.0, 50.0, -10.0, datetime.date.today(), False, None),
        (40.0, 50.0, -10.0, datetime.date.today(), False, None),
    ]
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "daily PnL" in r.reason


def test_drawdown_triggers_halt(monkeypatch):
    _enable_live(monkeypatch)
    # Wallet $30, пик был $50 — просадка $20 ≥ TOTAL_DRAWDOWN_LIMIT_USD=$15
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = None
    cursor.fetchone.side_effect = [
        (30.0, 50.0, 0, datetime.date.today(), False, None),
        (30.0, 50.0, 0, datetime.date.today(), False, None),
    ]
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "drawdown" in r.reason


def test_too_many_concurrent_positions(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap, open_positions=1)  # уже 1 открыта, лимит 1
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "positions open" in r.reason


def test_max_trades_per_day(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap, trades_today=20)  # достигли лимита
    r = check_can_open(conn, proposed_position_usd=5.0)
    assert not r.allowed
    assert "today" in r.reason


def test_low_pool_liquidity_blocked(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap)
    r = check_can_open(conn, proposed_position_usd=5.0, pool_liquidity_usd=5_000.0)
    assert not r.allowed
    assert "liquidity" in r.reason


def test_slippage_over_limit_blocked(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap)
    r = check_can_open(conn, proposed_position_usd=5.0,
                       pool_liquidity_usd=100_000.0,
                       estimated_slippage_bps=500)
    assert not r.allowed
    assert "slippage" in r.reason


def test_all_checks_pass(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, 0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap)
    r = check_can_open(conn, proposed_position_usd=5.0,
                       pool_liquidity_usd=100_000.0,
                       estimated_slippage_bps=100)
    assert r.allowed
    assert r.reason == "ok"


def test_snapshot_contains_state(monkeypatch):
    _enable_live(monkeypatch)
    snap = CapitalSnapshot(50.0, 50.0, -2.0, datetime.date.today(), False, None)
    conn = _conn_with_state(snap, open_positions=0, trades_today=3)
    r = check_can_open(conn, proposed_position_usd=5.0,
                       pool_liquidity_usd=100_000.0,
                       estimated_slippage_bps=100)
    assert r.allowed
    assert r.snapshot["daily_pnl_usd"] == -2.0
    assert r.snapshot["open_positions"] == 0
    assert r.snapshot["trades_today"] == 3
