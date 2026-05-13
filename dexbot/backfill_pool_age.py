"""Бэкфилл pool_age_at_signal_min для существующих wallet_signals.

Стратегия:
1. Группируем сигналы по token_address — для каждого уникального mint один lookup.
2. Через onchain.resolve_creation_ts() — DexScreener первичный, Helius fallback,
   с двухуровневым кэшем.
3. Для каждого сигнала на этом mint считаем pool_age = block_time - creation_ts.
4. Идём пачками, можно прерывать и возобновлять (берём только NULL'ы).

CLI:
    python -m dexbot.backfill_pool_age              # бэкфилл всех null'ов
    python -m dexbot.backfill_pool_age --core-only  # только сигналы от is_core кошельков
    python -m dexbot.backfill_pool_age --limit 500  # тестовый прогон на 500 уникальных mints
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from . import db, onchain
from .config import load_config

log = logging.getLogger("dexbot.backfill")


def fetch_pending_tokens(conn, core_only: bool, limit: int | None) -> list[str]:
    """Уникальные token_address из wallet_signals где pool_age_at_signal_min IS NULL."""
    join_clause = (
        "JOIN watched_wallets w ON w.address = s.wallet AND w.is_core = TRUE"
        if core_only else ""
    )
    sql = f"""
        SELECT DISTINCT s.token_address
        FROM wallet_signals s
        {join_clause}
        WHERE s.pool_age_at_signal_min IS NULL
          AND s.chain = 'solana'
        ORDER BY s.token_address
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return [r[0] for r in cur.fetchall()]


def update_signals_for_token(conn, token_address: str, creation_ts: int) -> int:
    """Записывает pool_age во все сигналы этого токена. Returns rows updated."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE wallet_signals
            SET pool_age_at_signal_min = GREATEST(0,
                (EXTRACT(EPOCH FROM block_time)::INT - %s) / 60)
            WHERE chain = 'solana'
              AND token_address = %s
              AND pool_age_at_signal_min IS NULL
            """,
            (creation_ts, token_address),
        )
        return cur.rowcount


def run(database_url: str, *, core_only: bool, limit: int | None) -> None:
    with db.connect(database_url) as conn:
        tokens = fetch_pending_tokens(conn, core_only=core_only, limit=limit)
        log.info("найдено %d уникальных токенов для бэкфилла (core_only=%s)",
                 len(tokens), core_only)

        resolved = 0
        unknown = 0
        signals_updated = 0
        started = time.time()

        for i, token in enumerate(tokens, 1):
            creation_ts = onchain.resolve_creation_ts(token, conn=conn)
            conn.commit()  # save pool_metadata immediately

            if creation_ts is None:
                unknown += 1
                if i % 20 == 0:
                    elapsed = time.time() - started
                    log.info("  %d/%d (%.1f/sec): resolved=%d unknown=%d signals_updated=%d",
                             i, len(tokens), i/elapsed, resolved, unknown, signals_updated)
                continue

            n = update_signals_for_token(conn, token, creation_ts)
            conn.commit()
            resolved += 1
            signals_updated += n

            if i % 20 == 0:
                elapsed = time.time() - started
                log.info("  %d/%d (%.1f/sec): resolved=%d unknown=%d signals_updated=%d",
                         i, len(tokens), i/elapsed, resolved, unknown, signals_updated)

        log.info("ГОТОВО: resolved=%d unknown=%d signals_updated=%d за %.0fс",
                 resolved, unknown, signals_updated, time.time() - started)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dexbot.backfill_pool_age")
    p.add_argument("--core-only", action="store_true",
                   help="Только сигналы от is_core=TRUE кошельков (~1k mints).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap на число уникальных mints — для тестовых прогонов.")
    args = p.parse_args(argv)

    config = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    run(config.database_url, core_only=args.core_only, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
