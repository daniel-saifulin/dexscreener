"""Postgres helpers (psycopg3). Used by screener and later watcher/monitor."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


@contextmanager
def connect(database_url: str):
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def run_migrations(database_url: str) -> None:
    """Apply all .sql files in migrations/ in lexical order. Idempotent —
    each migration uses CREATE TABLE IF NOT EXISTS / similar."""
    with connect(database_url) as conn, conn.cursor() as cur:
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            log.info("applying %s", sql_file.name)
            cur.execute(sql_file.read_text())
        conn.commit()


def upsert_safety_cache(conn, chain: str, address: str, flags: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO safety_cache (chain, token_address, fetched_at, flags)
            VALUES (%s, %s, NOW(), %s::jsonb)
            ON CONFLICT (chain, token_address)
            DO UPDATE SET fetched_at = EXCLUDED.fetched_at, flags = EXCLUDED.flags
            """,
            (chain, address, json.dumps(flags)),
        )


def fetch_safety_cache(conn, chain: str, address: str, max_age_minutes: int = 60) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT flags FROM safety_cache
            WHERE chain = %s AND token_address = %s
              AND fetched_at > NOW() - (%s || ' minutes')::interval
            """,
            (chain, address, str(max_age_minutes)),
        )
        row = cur.fetchone()
        return row[0] if row else None


def insert_candidate(
    conn,
    *,
    chain: str,
    token_address: str,
    pair_address: str,
    symbol: str | None,
    name: str | None,
    price_usd: float | None,
    liquidity_usd: float | None,
    volume_h1_usd: float | None,
    price_change_h1: float | None,
    pair_age_minutes: int | None,
    buys_h1: int | None,
    sells_h1: int | None,
    passed_filters: bool,
    filter_reasons: list[str],
    safety_flags: dict,
    raw: dict,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO candidates (
                chain, token_address, pair_address, symbol, name,
                price_usd, liquidity_usd, volume_h1_usd, price_change_h1,
                pair_age_minutes, buys_h1, sells_h1,
                passed_filters, filter_reasons, safety_flags, raw
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
            RETURNING id
            """,
            (
                chain, token_address, pair_address, symbol, name,
                price_usd, liquidity_usd, volume_h1_usd, price_change_h1,
                pair_age_minutes, buys_h1, sells_h1,
                passed_filters, json.dumps(filter_reasons), json.dumps(safety_flags),
                json.dumps(raw),
            ),
        )
        return cur.fetchone()[0]
