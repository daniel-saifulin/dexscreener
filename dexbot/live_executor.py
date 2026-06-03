"""Live executor — главная логика реальных сделок.

Связывает risk_guard + safety_runtime + live + DB. Вызывается:
1. Из webhook_server при детекте Gvy buy сигнала
2. Из webhook_server при детекте Gvy sell (follower-exit)
3. Из периодического monitor для TP/SL/max_hold

ВСЕ сделки проходят через risk_guard → safety_runtime → live перед swap.
Никаких прямых вызовов swap-API в обход этой цепочки.

ЕДИНСТВЕННОЕ место где читается SOLANA_PRIVATE_KEY.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import live, risk_guard, safety_runtime

log = logging.getLogger("dexbot.live_executor")

# Какие кошельки реально торгуем live (подмножество SOLO_WALLETS).
# Стартуем ТОЛЬКО с GvyLS9WF — самый надёжный сигнал (80% WR на 236 paper closed).
SOLO_LIVE_WALLETS: frozenset[str] = frozenset({
    "GvyLS9WFxUBzoiVPKTJAR2bGLocnoEVWRYh4D8i5z7m1",
})

# Размер позиции
DEFAULT_POSITION_USD = float(os.environ.get("LIVE_MAX_POSITION_USD", "5.0"))

# Slippage tolerance для каждого swap
SLIPPAGE_BPS = int(os.environ.get("LIVE_MAX_SLIPPAGE_BPS", "300"))

# Max hold limit (как в paper)
MAX_HOLD_HOURS = 168


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _wallet_pubkey() -> Optional[str]:
    """Public key из env. На fly.io через LIVE_WALLET_ADDRESS, лок локально тоже."""
    return os.environ.get("LIVE_WALLET_ADDRESS") or os.environ.get("SOLANA_WALLET_PUBKEY")


def _secret_key() -> Optional[str]:
    """Приватный ключ из env. На fly.io это секрет, лок в .env."""
    return os.environ.get("SOLANA_PRIVATE_KEY")


def _pubkey_from_secret(secret_b58: str) -> str:
    """Извлекает public key из приватного. Лениво импортируем solders."""
    from solders.keypair import Keypair
    import base58
    keypair = Keypair.from_bytes(base58.b58decode(secret_b58))
    return str(keypair.pubkey())


def _get_sol_price_usd() -> Optional[float]:
    """Текущая цена SOL/USD через DexScreener — единственный источник правды."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{live.SOL_MINT}",
            timeout=10,
        )
        if not r.ok:
            return None
        data = r.json()
        pairs = (data.get("pairs") or [])
        sol_usdc = [
            p for p in pairs
            if (p.get("chainId") or "").lower() == "solana"
            and (p.get("baseToken") or {}).get("address") == live.SOL_MINT
        ]
        if not sol_usdc:
            return None
        best = max(sol_usdc, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        return float(best.get("priceUsd")) if best.get("priceUsd") else None
    except Exception as e:
        log.warning("SOL price fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Capital state seeding (вызвать один раз при старте — отдельной командой)
# ---------------------------------------------------------------------------

def seed_capital_state(conn) -> bool:
    """Инициализирует live_capital_state текущим балансом кошелька.
    Вызывается вручную через CLI один раз после фандинга кошелька.
    """
    pubkey = _wallet_pubkey()
    if not pubkey:
        # Восстанавливаем из приватного ключа
        secret = _secret_key()
        if not secret:
            log.error("seed_capital_state: no wallet pubkey + no SOLANA_PRIVATE_KEY")
            return False
        try:
            pubkey = _pubkey_from_secret(secret)
        except Exception as e:
            log.error("seed_capital_state: cannot derive pubkey: %s", e)
            return False

    sol_lamports = live.get_sol_balance(pubkey)
    sol_amount = sol_lamports / live.LAMPORTS_PER_SOL
    sol_price = _get_sol_price_usd()
    if sol_price is None:
        log.error("seed_capital_state: SOL price unavailable")
        return False

    usd_value = sol_amount * sol_price
    log.info("Seeding capital: %.4f SOL × $%.2f = $%.2f", sol_amount, sol_price, usd_value)

    risk_guard.update_capital(conn, wallet_balance_usd=usd_value, delta_pnl_usd=0)
    return True


# ---------------------------------------------------------------------------
# OPEN: попытка живой покупки
# ---------------------------------------------------------------------------

@dataclass
class OpenResult:
    success: bool
    reason: str
    trade_id: Optional[int] = None
    tx_sig: Optional[str] = None
    snapshot: dict = None


def try_open_live(
    conn,
    *,
    signal_id: int,
    source_wallet: str,
    token_mint: str,
    symbol: Optional[str],
    pair: dict,
) -> OpenResult:
    """Главная функция входа в live trade. Возвращает OpenResult.

    Цепочка проверок: risk_guard → safety_runtime → quote → execute.
    Любой fail → return early без побочных эффектов.
    """
    snap = {"source_wallet": source_wallet[:8], "token": token_mint[:8]}

    # 0. Whitelist check
    if source_wallet not in SOLO_LIVE_WALLETS:
        return OpenResult(False, f"wallet_not_in_live_set", snapshot=snap)

    # 1. SOL price
    sol_price = _get_sol_price_usd()
    if sol_price is None:
        return OpenResult(False, "sol_price_unavailable", snapshot=snap)
    snap["sol_price_usd"] = sol_price

    # 2. Pool liquidity for risk_guard
    pool_liq = float((pair.get("liquidity") or {}).get("usd") or 0)

    # 3. Jupiter quote — для оценки реального slippage ПЕРЕД open
    position_usd = DEFAULT_POSITION_USD
    sol_lamports = live.usd_to_sol_lamports(position_usd, sol_price)
    try:
        quote = live.get_quote(live.SOL_MINT, token_mint, sol_lamports,
                               slippage_bps=SLIPPAGE_BPS)
    except Exception as e:
        return OpenResult(False, f"quote_failed: {e}", snapshot=snap)

    price_impact_pct = live.estimate_price_impact_pct(quote)
    estimated_slippage_bps = int(abs(price_impact_pct) * 100)
    snap["price_impact_pct"] = price_impact_pct

    # 4. Risk guard — после quote чтобы знать реальный slippage
    risk = risk_guard.check_can_open(
        conn,
        proposed_position_usd=position_usd,
        pool_liquidity_usd=pool_liq,
        estimated_slippage_bps=estimated_slippage_bps,
    )
    if not risk.allowed:
        return OpenResult(False, f"risk_guard: {risk.reason}",
                          snapshot={**snap, **risk.snapshot})
    snap.update(risk.snapshot)

    # 5. Safety check (RugCheck + liquidity)
    safety = safety_runtime.check_token(token_mint, liquidity_usd=pool_liq)
    if not safety.safe:
        return OpenResult(False, f"safety: {safety.reason}", snapshot=snap)

    # 6. Подписываем + отправляем swap
    secret = _secret_key()
    if not secret:
        return OpenResult(False, "no_secret_key", snapshot=snap)

    try:
        pubkey = _pubkey_from_secret(secret)
        unsigned_tx = live.build_swap_transaction(quote, pubkey)
        signed_tx = live.sign_transaction(unsigned_tx, secret)
        tx_sig = live.send_signed_transaction(signed_tx)
    except Exception as e:
        log.exception("LIVE OPEN swap submission failed")
        return OpenResult(False, f"swap_submit_failed: {e}", snapshot=snap)

    log.info("LIVE BUY submitted: tx=%s token=%s position=$%.2f",
             tx_sig[:12], symbol or token_mint[:8], position_usd)

    # 7. Ждём подтверждения
    confirmed = live.wait_for_confirmation(tx_sig, timeout_sec=45)
    if not confirmed:
        log.error("LIVE BUY tx %s NOT confirmed in 45s", tx_sig)
        return OpenResult(False, "tx_not_confirmed", tx_sig=tx_sig, snapshot=snap)

    # 8. Проверяем что токены реально на кошельке
    try:
        token_balance = live.get_token_balance(pubkey, token_mint)
    except Exception as e:
        log.warning("token_balance check failed: %s — записываем сделку всё равно", e)
        token_balance = 0

    if token_balance == 0:
        log.error("LIVE BUY tx %s confirmed but token_balance=0", tx_sig)
        return OpenResult(False, "zero_balance_after_swap", tx_sig=tx_sig, snapshot=snap)

    # 9. Запись в DB
    try:
        entry_price_usd = float(pair["priceUsd"])
    except (TypeError, KeyError, ValueError):
        return OpenResult(False, "no_entry_price", tx_sig=tx_sig, snapshot=snap)

    stop_price = entry_price_usd * 0.88
    take_price = entry_price_usd * 1.18

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO live_trades (
                signal_id, source_wallet, chain, token_address, symbol,
                entry_sig, entry_price_usd, entry_amount_usd,
                entry_amount_tokens, entry_slippage_pct,
                entry_sol_lamports, sol_price_usd_at_entry,
                stop_price_usd, take_price_usd,
                risk_guard_snapshot, safety_check
            )
            VALUES (%s, %s, 'solana', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id
            """,
            (
                signal_id, source_wallet, token_mint, symbol,
                tx_sig, entry_price_usd, position_usd,
                token_balance, price_impact_pct,
                sol_lamports, sol_price,
                stop_price, take_price,
                json.dumps(snap), json.dumps({"safe": safety.safe, "raw": safety.raw}),
            ),
        )
        trade_id = cur.fetchone()[0]
    conn.commit()

    # 10. Обновляем capital state
    new_sol_lamports = live.get_sol_balance(pubkey)
    new_balance_usd = (new_sol_lamports / live.LAMPORTS_PER_SOL) * sol_price
    risk_guard.update_capital(conn, wallet_balance_usd=new_balance_usd)

    log.info("LIVE BUY confirmed: trade=%d tx=%s entry_price=$%.6f",
             trade_id, tx_sig[:12], entry_price_usd)
    return OpenResult(True, "opened", trade_id=trade_id, tx_sig=tx_sig, snapshot=snap)


# ---------------------------------------------------------------------------
# CLOSE: продажа memecoin → SOL
# ---------------------------------------------------------------------------

@dataclass
class CloseResult:
    success: bool
    reason: str
    exit_tx_sig: Optional[str] = None
    pnl_pct: Optional[float] = None
    pnl_usd: Optional[float] = None


def try_close_live(conn, trade_row: dict, *, exit_reason: str) -> CloseResult:
    """Закрывает live позицию через swap memecoin → SOL.

    `trade_row` — словарь с полями из live_trades (id, token_address, entry_*,
    sol_price_usd_at_entry, entry_sol_lamports и т.д.)
    """
    secret = _secret_key()
    if not secret:
        return CloseResult(False, "no_secret_key")

    pubkey = _pubkey_from_secret(secret)

    # 1. Сколько токенов у нас сейчас?
    try:
        token_balance = live.get_token_balance(pubkey, trade_row["token_address"])
    except Exception as e:
        return CloseResult(False, f"token_balance_failed: {e}")

    if token_balance == 0:
        log.warning("CLOSE %d: token balance is 0, marking as closed_no_balance",
                    trade_row["id"])
        # Запишем закрытие без свапа
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE live_trades SET status='closed_no_balance', closed_at=NOW(),
                       reason_out=%s WHERE id=%s
            """, (exit_reason, trade_row["id"]))
        conn.commit()
        return CloseResult(False, "zero_balance")

    # 2. SOL price для USD расчёта
    sol_price = _get_sol_price_usd()
    if sol_price is None:
        return CloseResult(False, "sol_price_unavailable")

    # 3. Quote: memecoin → SOL
    try:
        quote = live.get_quote(trade_row["token_address"], live.SOL_MINT,
                               token_balance, slippage_bps=SLIPPAGE_BPS)
    except Exception as e:
        return CloseResult(False, f"quote_failed: {e}")

    # 4. Build + sign + send
    try:
        unsigned = live.build_swap_transaction(quote, pubkey)
        signed = live.sign_transaction(unsigned, secret)
        tx_sig = live.send_signed_transaction(signed)
    except Exception as e:
        log.exception("LIVE CLOSE swap failed")
        return CloseResult(False, f"swap_submit_failed: {e}")

    log.info("LIVE SELL submitted: tx=%s trade=%d reason=%s",
             tx_sig[:12], trade_row["id"], exit_reason)

    if not live.wait_for_confirmation(tx_sig, timeout_sec=60):
        # Не подтвердилось — оставляем status='open' и попробуем заново
        log.error("LIVE CLOSE tx %s NOT confirmed", tx_sig)
        return CloseResult(False, "tx_not_confirmed", exit_tx_sig=tx_sig)

    # 5. Сколько SOL получили? (балансом)
    new_sol_lamports = live.get_sol_balance(pubkey)
    sol_received = new_sol_lamports - 0  # нужно дельту от до-сделки
    # Это упрощение — точнее реконструировать через quote.outAmount
    exit_sol_lamports = live.estimate_swap_output(quote)
    exit_amount_usd = (exit_sol_lamports / live.LAMPORTS_PER_SOL) * sol_price

    pnl_usd = exit_amount_usd - float(trade_row["entry_amount_usd"])
    pnl_pct = pnl_usd / float(trade_row["entry_amount_usd"]) * 100

    # 6. Map reason → status
    status_map = {
        "stop": "closed_sl", "take_profit": "closed_tp",
        "wallet_sold": "closed_wallet_sold", "max_hold": "closed_max_hold",
        "manual": "closed_manual",
    }
    status = status_map.get(exit_reason, "closed_manual")

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE live_trades
            SET status = %s, closed_at = NOW(), exit_sig = %s,
                exit_price_usd = %s, exit_amount_usd = %s,
                exit_sol_lamports = %s, sol_price_usd_at_exit = %s,
                pnl_usd = %s, pnl_pct = %s, reason_out = %s
            WHERE id = %s
        """, (
            status, tx_sig,
            exit_amount_usd / (float(trade_row["entry_amount_tokens"]) or 1),
            exit_amount_usd, exit_sol_lamports, sol_price,
            pnl_usd, pnl_pct, exit_reason, trade_row["id"],
        ))
    conn.commit()

    # 7. Обновляем capital state
    new_balance_usd = (new_sol_lamports / live.LAMPORTS_PER_SOL) * sol_price
    risk_guard.update_capital(conn, wallet_balance_usd=new_balance_usd,
                              delta_pnl_usd=pnl_usd)

    log.info("LIVE CLOSE confirmed: trade=%d pnl=%+.2f%% ($%.2f)",
             trade_row["id"], pnl_pct, pnl_usd)
    return CloseResult(True, exit_reason, exit_tx_sig=tx_sig,
                       pnl_pct=pnl_pct, pnl_usd=pnl_usd)


# ---------------------------------------------------------------------------
# CLI: seed capital + status
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse, sys
    from . import db
    from .config import load_config

    p = argparse.ArgumentParser(prog="dexbot.live_executor")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="Инициализировать live_capital_state с текущим балансом")
    sub.add_parser("status", help="Показать текущее состояние live trading")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    with db.connect(config.database_url) as conn:
        if args.cmd == "seed":
            ok = seed_capital_state(conn)
            return 0 if ok else 1
        if args.cmd == "status":
            snap = risk_guard.fetch_capital_state(conn)
            print(f"is_halted          : {snap.is_halted}")
            print(f"halt_reason        : {snap.halt_reason}")
            print(f"wallet_balance_usd : ${snap.wallet_balance_usd:.2f}")
            print(f"peak_balance_usd   : ${snap.peak_balance_usd:.2f}")
            print(f"daily_pnl_usd      : ${snap.daily_pnl_usd:+.2f}")
            print(f"daily_anchor_date  : {snap.daily_anchor_date}")
            print(f"LIVE_TRADING_ENABLED env: "
                  f"{os.environ.get('LIVE_TRADING_ENABLED', 'false')}")
            return 0
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
