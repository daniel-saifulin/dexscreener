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

# Сколько токенов обрабатывать между открытиями коннекта к Neon.
# Коннект держится секунды на батч, не часы на весь прогон —
# иначе Neon рвёт idle или исчерпываются ephemeral порты.
BATCH_SIZE = 50


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


def preload_metadata_cache(conn) -> int:
    """Загружает весь pool_metadata в onchain._L1_CACHE одним SELECT.
    Возвращает число загруженных адресов."""
    with conn.cursor() as cur:
        cur.execute("SELECT address, creation_ts FROM pool_metadata")
        n = 0
        for address, ts in cur.fetchall():
            onchain._L1_CACHE[address] = int(ts.timestamp()) if ts is not None else None
            n += 1
    return n


def flush_batch(database_url: str, batch: list[tuple[str, int | None, str]]) -> int:
    """Записывает накопленный батч: pool_metadata + update wallet_signals.
    Коннект открыт ровно на длительность SQL — секунды, не часы.
    Возвращает суммарное число обновлённых сигналов."""
    if not batch:
        return 0
    signals_updated = 0
    with db.connect(database_url) as conn, conn.cursor() as cur:
        for address, creation_ts, source in batch:
            cur.execute(
                """
                INSERT INTO pool_metadata (address, creation_ts, source, error)
                VALUES (%s, to_timestamp(%s), %s, NULL)
                ON CONFLICT (address) DO UPDATE
                  SET creation_ts = EXCLUDED.creation_ts,
                      source      = EXCLUDED.source,
                      error       = EXCLUDED.error
                """,
                (address, creation_ts, source),
            )
            if creation_ts is not None:
                cur.execute(
                    """
                    UPDATE wallet_signals
                    SET pool_age_at_signal_min = GREATEST(0,
                        (EXTRACT(EPOCH FROM block_time)::INT - %s) / 60)
                    WHERE chain = 'solana'
                      AND token_address = %s
                      AND pool_age_at_signal_min IS NULL
                    """,
                    (creation_ts, address),
                )
                signals_updated += cur.rowcount
        conn.commit()
    return signals_updated


def run(database_url: str, *, core_only: bool, limit: int | None) -> None:
    # 1) Короткий коннект: список pending токенов + preload L2 кэша.
    with db.connect(database_url) as conn:
        tokens = fetch_pending_tokens(conn, core_only=core_only, limit=limit)
        cached = preload_metadata_cache(conn)
    log.info("найдено %d уникальных токенов для бэкфилла (core_only=%s); preload pool_metadata=%d",
             len(tokens), core_only, cached)

    resolved = 0
    unknown = 0
    signals_updated = 0
    started = time.time()
    batch: list[tuple[str, int | None, str]] = []

    # 2) Цикл без открытого коннекта: только HTTP вызовы DexScreener/Helius.
    #    Результаты копятся в батч; каждые BATCH_SIZE — открываем коннект и flush.
    for i, token in enumerate(tokens, 1):
        # Если уже в L1 кэше (preloaded) — пропускаем, ничего не пишем.
        if token in onchain._L1_CACHE:
            ts = onchain._L1_CACHE[token]
            if ts is None:
                unknown += 1
            else:
                # Сигналы могли быть NULL хотя кэш есть — обновим в батче.
                batch.append((token, ts, "cache"))
                resolved += 1
        else:
            ts, source = _resolve_no_db(token)
            batch.append((token, ts, source))
            if ts is None:
                unknown += 1
            else:
                resolved += 1

        if len(batch) >= BATCH_SIZE:
            signals_updated += flush_batch(database_url, batch)
            batch.clear()

        if i % 20 == 0:
            elapsed = time.time() - started
            log.info("  %d/%d (%.1f/sec): resolved=%d unknown=%d signals_updated=%d",
                     i, len(tokens), i/elapsed, resolved, unknown, signals_updated)

    # 3) Финальный flush остатка батча.
    signals_updated += flush_batch(database_url, batch)

    log.info("ГОТОВО: resolved=%d unknown=%d signals_updated=%d за %.0fс",
             resolved, unknown, signals_updated, time.time() - started)


def _resolve_no_db(address: str) -> tuple[int | None, str]:
    """Аналог onchain.resolve_creation_ts но без conn — только HTTP.
    Возвращает (creation_ts | None, source). Пишет в L1 кэш."""
    ts = onchain._dexscreener_pair_created_ts(address)
    source = "dexscreener"
    if ts is None:
        try:
            ts = onchain._helius_oldest_signature_ts(address)
            source = "helius" if ts is not None else "helius_miss"
        except Exception as e:
            ts = None
            source = "error"
            log.warning("helius fallback failed for %s: %s", address[:8], e)
    onchain._L1_CACHE[address] = ts
    return ts, source


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
