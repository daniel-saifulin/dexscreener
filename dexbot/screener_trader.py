"""Гипотеза C: screener как независимая paper-trading стратегия.

Идея: проверить даёт ли screener `passed_filters=TRUE` сам по себе прибыльную
стратегию, БЕЗ всякой связи с core-wallets. Отдельная когорта, отдельная таблица
`screener_paper_trades`, отдельная аналитика.

ИЗОЛЯЦИЯ:
- Не дедуплицируется с wallet_paper_trades. Если токен попал в обе стратегии —
  обе открывают свои сделки.
- Не trough-влияет на core-стратегию. Core trading остаётся в watcher.py +
  webhook_server.py, не трогаем.

Параметры (как у core-стратегии для прямого сравнения):
- TP +18% / SL −12%
- 168-часовой max_hold лимит
- Дедуп внутри когорты: один токен — одна open screener-сделка за 24 часа.

Выход:
- TP / SL: monitor batching в watcher.py monitor_pass (или в screener.yml)
- max_hold: после 168ч в open
- БЕЗ follower-exit (нет wallet-источника)
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import db, dexscreener

log = logging.getLogger("dexbot.screener_trader")

TP_PCT = 18.0
SL_PCT = -12.0
TIMEOUT_HOURS = 168
NEW_CANDIDATE_WINDOW_MIN = 30   # рассматриваем passed-кандидатов за последние N минут
DEDUP_HOURS = 24                # один токен — одна открытая screener-сделка в 24ч


# ---------------------------------------------------------------------------
# Open new trades
# ---------------------------------------------------------------------------

def fetch_new_passed_candidates(conn) -> list[dict]:
    """Уникальные passed-кандидаты за последние N минут на которые нет открытой
    screener-сделки в последние 24 часа (и нет закрытой — чтобы не реоткрывать
    то что уже сработало по TP/SL).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (c.chain, c.token_address)
                   c.id, c.chain, c.token_address, c.symbol, c.price_usd
            FROM candidates c
            WHERE c.passed_filters = TRUE
              AND c.detected_at >= NOW() - INTERVAL '{NEW_CANDIDATE_WINDOW_MIN} minutes'
              AND c.price_usd IS NOT NULL
              AND c.price_usd > 0
              AND NOT EXISTS (
                SELECT 1 FROM screener_paper_trades p
                WHERE p.chain = c.chain
                  AND p.token_address = c.token_address
                  AND p.opened_at >= NOW() - INTERVAL '{DEDUP_HOURS} hours'
              )
            ORDER BY c.chain, c.token_address, c.detected_at DESC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_current_price(chain: str, token_address: str) -> tuple[float | None, str | None]:
    pairs = dexscreener.fetch_pairs_for_token(token_address)
    chain_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain]
    pair = dexscreener.best_pair(chain_pairs)
    if not pair or not pair.get("priceUsd"):
        return None, None
    try:
        price = float(pair["priceUsd"])
    except (TypeError, ValueError):
        return None, None
    symbol = (pair.get("baseToken") or {}).get("symbol")
    return price, symbol


def open_screener_trades(database_url: str) -> tuple[int, int]:
    """Returns (opened, skipped). Открывает сделки на свежие passed-кандидаты."""
    opened = 0
    skipped = 0
    with db.connect(database_url) as conn:
        candidates = fetch_new_passed_candidates(conn)
        log.info("найдено %d свежих passed-кандидатов для screener-cohort", len(candidates))

        for c in candidates:
            price, fresh_symbol = _fetch_current_price(c["chain"], c["token_address"])
            if price is None or price <= 0:
                skipped += 1
                continue
            stop = price * (1 + SL_PCT / 100.0)
            take = price * (1 + TP_PCT / 100.0)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO screener_paper_trades (
                        candidate_id, chain, token_address, symbol,
                        entry_price_usd, stop_price_usd, take_price_usd
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        c["id"], c["chain"], c["token_address"],
                        c.get("symbol") or fresh_symbol,
                        price, stop, take,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            if row:
                opened += 1
                log.info("  OPENED screener trade %s @ %.8f (token=%s)",
                         (c.get("symbol") or fresh_symbol or c["token_address"][:8]),
                         price, c["token_address"][:8])
    return opened, skipped


# ---------------------------------------------------------------------------
# Monitor open trades (TP / SL / max_hold)
# ---------------------------------------------------------------------------

def monitor_screener_trades(database_url: str) -> int:
    """Returns count of trades closed this pass. Batched DexScreener prices."""
    closed = 0
    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, chain, token_address, symbol, entry_price_usd,
                       stop_price_usd, take_price_usd, opened_at
                FROM screener_paper_trades
                WHERE status = 'open'
                ORDER BY opened_at
                """
            )
            trades = cur.fetchall()

        if not trades:
            return 0
        log.info("monitoring %d open screener trades", len(trades))

        # Batch prices
        by_chain: dict[str, list] = {}
        for t in trades:
            by_chain.setdefault(t[1], []).append(t)

        pair_by_token: dict[str, dict] = {}
        for chain, chain_trades in by_chain.items():
            addrs = list({t[2] for t in chain_trades})
            pairs_map = dexscreener.fetch_pairs_for_tokens(addrs)
            for addr, pair_list in pairs_map.items():
                in_chain = [p for p in pair_list if (p.get("chainId") or "").lower() == chain]
                best = dexscreener.best_pair(in_chain)
                if best:
                    pair_by_token[addr] = best

        now = time.time()
        for trade_id, chain, addr, symbol, entry, stop, take, opened_at in trades:
            pair = pair_by_token.get(addr)
            if not pair or not pair.get("priceUsd"):
                continue
            try:
                cur_price = float(pair["priceUsd"])
            except (TypeError, ValueError):
                continue

            age_hours = (now - opened_at.timestamp()) / 3600
            exit_price: float | None = None
            status: str | None = None
            reason: str | None = None

            if cur_price <= float(stop):
                exit_price, status, reason = cur_price, "closed_sl", "stop"
            elif cur_price >= float(take):
                exit_price, status, reason = cur_price, "closed_tp", "take_profit"
            elif age_hours > TIMEOUT_HOURS:
                exit_price, status, reason = cur_price, "closed_max_hold", "max_hold_168h"

            if exit_price is None:
                continue

            pnl_pct = (exit_price - float(entry)) / float(entry) * 100.0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE screener_paper_trades
                    SET closed_at = NOW(), exit_price_usd = %s, status = %s,
                        pnl_pct = %s, reason_out = %s
                    WHERE id = %s AND status = 'open'
                    """,
                    (exit_price, status, pnl_pct, reason, trade_id),
                )
            closed += 1
            log.info("  closed #%d %s: %s pnl=%+.1f%%",
                     trade_id, symbol or addr[:8], status, pnl_pct)
        conn.commit()
    return closed


# ---------------------------------------------------------------------------
# CLI for ad-hoc usage
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    import sys
    from .config import load_config

    p = argparse.ArgumentParser(prog="dexbot.screener_trader")
    p.add_argument("--monitor-only", action="store_true")
    p.add_argument("--open-only", action="store_true")
    args = p.parse_args()

    config = load_config()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    if not args.monitor_only:
        opened, skipped = open_screener_trades(config.database_url)
        print(f"screener-trader: opened={opened} skipped={skipped}")
    if not args.open_only:
        n = monitor_screener_trades(config.database_url)
        print(f"screener-trader monitor: closed={n}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
