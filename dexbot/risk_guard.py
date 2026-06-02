"""Risk guard для live-торговли.

Все защиты централизованы здесь. Перед КАЖДЫМ открытием реальной сделки
вызывается `check_can_open()`, который возвращает `(allowed, reason)`.

ВСЕ значения в USD (не SOL) для последовательности. Конвертация делается
вверху стека (live.py).

Дизайн:
- Чистые функции (большая часть) — никаких side-effects в принятии решений
- Состояние читается из БД (live_capital_state) одной командой
- При нарушении лимита halt записывается в БД и в логи
- Manual reset халта только через прямое UPDATE — нельзя «случайно» снять
"""
from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("dexbot.risk_guard")


# ---------------------------------------------------------------------------
# Конфигурация защит (можно переопределить через env, но дефолты консервативны)
# ---------------------------------------------------------------------------

# Максимальный размер ОДНОЙ позиции в USD
MAX_POSITION_USD: float = float(os.environ.get("LIVE_MAX_POSITION_USD", "5.0"))

# Сколько одновременных позиций (на старте — 1)
MAX_CONCURRENT_POSITIONS: int = int(os.environ.get("LIVE_MAX_CONCURRENT", "1"))

# Лимит потерь за день (по дням UTC). При -$10 → halt до утра.
DAILY_LOSS_LIMIT_USD: float = float(os.environ.get("LIVE_DAILY_LOSS_USD", "10.0"))

# Cumulative drawdown от пика капитала. При просадке $15 — halt полностью.
TOTAL_DRAWDOWN_LIMIT_USD: float = float(os.environ.get("LIVE_DRAWDOWN_USD", "15.0"))

# Максимум сделок за день (защита от каскадных багов)
MAX_TRADES_PER_DAY: int = int(os.environ.get("LIVE_MAX_TRADES_DAY", "20"))

# Slippage cap при квоте Jupiter — отказываем входить если price impact больше
MAX_SLIPPAGE_BPS: int = int(os.environ.get("LIVE_MAX_SLIPPAGE_BPS", "300"))  # 3%

# Минимальная ликвидность пула для входа (отказ если меньше)
MIN_POOL_LIQUIDITY_USD: float = float(os.environ.get("LIVE_MIN_LIQUIDITY", "30000.0"))

# Главный kill-switch. Если FALSE — никакие сделки не открываются вообще.
LIVE_TRADING_ENABLED: bool = os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"


@dataclass
class CapitalSnapshot:
    """Текущее состояние капитала из live_capital_state."""
    wallet_balance_usd: float
    peak_balance_usd: float
    daily_pnl_usd: float
    daily_anchor_date: Optional[datetime.date]
    is_halted: bool
    halt_reason: Optional[str]


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str
    snapshot: dict


# ---------------------------------------------------------------------------
# Чтение состояния
# ---------------------------------------------------------------------------

def fetch_capital_state(conn) -> CapitalSnapshot:
    """Читает текущее состояние из live_capital_state. Если строки нет — создаёт пустую."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wallet_balance_usd, peak_balance_usd, daily_pnl_usd,
                   daily_anchor_date, is_halted, halt_reason
            FROM live_capital_state ORDER BY snapshot_at DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return CapitalSnapshot(0.0, 0.0, 0.0, None, False, None)
    return CapitalSnapshot(
        wallet_balance_usd=float(row[0] or 0),
        peak_balance_usd=float(row[1] or 0),
        daily_pnl_usd=float(row[2] or 0),
        daily_anchor_date=row[3],
        is_halted=bool(row[4]),
        halt_reason=row[5],
    )


def count_open_live_positions(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM live_trades WHERE status = 'open'")
        return int(cur.fetchone()[0])


def count_today_trades(conn) -> int:
    """Сколько live-сделок открыто СЕГОДНЯ (UTC)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM live_trades
            WHERE opened_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
            """
        )
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Запись состояния
# ---------------------------------------------------------------------------

def set_halt(conn, reason: str) -> None:
    """Активирует kill switch. Только ручной reset через UPDATE."""
    log.error("RISK GUARD HALT: %s", reason)
    snap = fetch_capital_state(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO live_capital_state
              (wallet_balance_usd, peak_balance_usd, daily_pnl_usd,
               daily_anchor_date, is_halted, halt_reason)
            VALUES (%s, %s, %s, %s, TRUE, %s)
            """,
            (snap.wallet_balance_usd, snap.peak_balance_usd, snap.daily_pnl_usd,
             snap.daily_anchor_date, reason),
        )
        conn.commit()


def update_capital(
    conn,
    *,
    wallet_balance_usd: float,
    delta_pnl_usd: Optional[float] = None,
) -> None:
    """Записывает свежее состояние после сделки или периодической проверки."""
    snap = fetch_capital_state(conn)
    today = datetime.date.today()

    if snap.daily_anchor_date != today:
        # Новый день UTC — сбрасываем daily_pnl
        new_daily_pnl = 0.0
        anchor = today
    else:
        new_daily_pnl = snap.daily_pnl_usd + (delta_pnl_usd or 0.0)
        anchor = snap.daily_anchor_date

    new_peak = max(snap.peak_balance_usd, wallet_balance_usd)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO live_capital_state
              (wallet_balance_usd, peak_balance_usd, daily_pnl_usd,
               daily_anchor_date, is_halted, halt_reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (wallet_balance_usd, new_peak, new_daily_pnl, anchor,
             snap.is_halted, snap.halt_reason),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Главная проверка
# ---------------------------------------------------------------------------

def check_can_open(
    conn,
    *,
    proposed_position_usd: float,
    pool_liquidity_usd: Optional[float] = None,
    estimated_slippage_bps: Optional[int] = None,
) -> RiskCheckResult:
    """ГЛАВНАЯ функция — её вызываем перед каждым swap'ом."""
    snap_dict = {}

    # 0. Global kill-switch
    if not LIVE_TRADING_ENABLED:
        return RiskCheckResult(False, "LIVE_TRADING_ENABLED=false", snap_dict)

    # 1. Состояние из БД
    snap = fetch_capital_state(conn)
    snap_dict = {
        "wallet_balance_usd": snap.wallet_balance_usd,
        "peak_balance_usd": snap.peak_balance_usd,
        "daily_pnl_usd": snap.daily_pnl_usd,
        "is_halted": snap.is_halted,
        "halt_reason": snap.halt_reason,
    }

    if snap.is_halted:
        return RiskCheckResult(False, f"halted: {snap.halt_reason}", snap_dict)

    # 2. Размер позиции
    if proposed_position_usd > MAX_POSITION_USD:
        return RiskCheckResult(False,
            f"position ${proposed_position_usd} > limit ${MAX_POSITION_USD}", snap_dict)

    # 3. Достаточно ли средств в кошельке
    if snap.wallet_balance_usd < proposed_position_usd:
        return RiskCheckResult(False,
            f"insufficient wallet balance ${snap.wallet_balance_usd:.2f} < ${proposed_position_usd}",
            snap_dict)

    # 4. Daily loss limit
    if -snap.daily_pnl_usd >= DAILY_LOSS_LIMIT_USD:
        set_halt(conn, f"daily loss limit hit: ${snap.daily_pnl_usd:.2f}")
        return RiskCheckResult(False,
            f"daily PnL ${snap.daily_pnl_usd:.2f} ≤ -${DAILY_LOSS_LIMIT_USD}", snap_dict)

    # 5. Total drawdown
    if snap.peak_balance_usd > 0:
        drawdown = snap.peak_balance_usd - snap.wallet_balance_usd
        if drawdown >= TOTAL_DRAWDOWN_LIMIT_USD:
            set_halt(conn, f"total drawdown hit: ${drawdown:.2f}")
            return RiskCheckResult(False,
                f"drawdown ${drawdown:.2f} ≥ ${TOTAL_DRAWDOWN_LIMIT_USD}", snap_dict)
        snap_dict["current_drawdown_usd"] = drawdown

    # 6. Max concurrent positions
    open_n = count_open_live_positions(conn)
    snap_dict["open_positions"] = open_n
    if open_n >= MAX_CONCURRENT_POSITIONS:
        return RiskCheckResult(False,
            f"{open_n} positions open ≥ {MAX_CONCURRENT_POSITIONS}", snap_dict)

    # 7. Max trades per day
    today_n = count_today_trades(conn)
    snap_dict["trades_today"] = today_n
    if today_n >= MAX_TRADES_PER_DAY:
        return RiskCheckResult(False,
            f"{today_n} trades today ≥ {MAX_TRADES_PER_DAY}", snap_dict)

    # 8. Pool liquidity
    if pool_liquidity_usd is not None and pool_liquidity_usd < MIN_POOL_LIQUIDITY_USD:
        return RiskCheckResult(False,
            f"pool liquidity ${pool_liquidity_usd:.0f} < ${MIN_POOL_LIQUIDITY_USD:.0f}",
            snap_dict)

    # 9. Slippage
    if estimated_slippage_bps is not None and estimated_slippage_bps > MAX_SLIPPAGE_BPS:
        return RiskCheckResult(False,
            f"slippage {estimated_slippage_bps}bps > {MAX_SLIPPAGE_BPS}bps", snap_dict)

    return RiskCheckResult(True, "ok", snap_dict)
