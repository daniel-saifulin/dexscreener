"""Harvest candidate seed wallets from on-chain data.

Strategy: take recently-detected Solana memecoin pools from `candidates`,
fetch each pool's recent SWAP transactions via Helius enhanced API, extract
the trader (`feePayer`). Wallets that traded N>=2 distinct memecoins are
real active swappers — bagholders and one-shot snipers fall out.

Helius scoring (run after harvest) further validates: an active trader
will show 30+ trades / 10+ distinct tokens over the 30-day window;
bots and one-shotters will not.
"""
from __future__ import annotations

import logging
import os
import time
from collections import Counter, defaultdict

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20

# Programs / system mints — these are not user wallets.
SKIP_OWNERS: frozenset[str] = frozenset({
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
})


def _api_key() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY not set")
    return key


def _fetch_pool_swaps(pool_address: str, *, limit: int = 100) -> list[dict]:
    r = requests.get(
        f"https://api.helius.xyz/v0/addresses/{pool_address}/transactions",
        params={"api-key": _api_key(), "limit": limit},
        timeout=DEFAULT_TIMEOUT,
    )
    if r.status_code != 200:
        return []
    data = r.json()
    return [tx for tx in (data if isinstance(data, list) else []) if tx.get("type") == "SWAP"]


def harvest_from_pools(
    conn,
    *,
    min_liquidity_usd: float = 30_000,
    max_liquidity_usd: float = 2_000_000,
    days: int = 14,
    max_pools: int = 30,
    swaps_per_pool: int = 100,
    min_distinct_tokens: int = 2,
    min_trades: int = 5,
    max_trades: int = 200,
) -> list[tuple[str, int, int]]:
    """Returns [(wallet, total_trades_seen, distinct_tokens_seen)] sorted by
    (distinct_tokens, total_trades) desc.

    Filters out:
      - Bots (>max_trades trades on our small sample → high-frequency wash)
      - One-shotters (<min_trades or <min_distinct_tokens)
      - System / router / program addresses
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (pair_address)
                pair_address, token_address, symbol, liquidity_usd
            FROM candidates
            WHERE chain = 'solana'
              AND pair_address <> ''
              AND liquidity_usd BETWEEN %s AND %s
              AND detected_at >= NOW() - (%s || ' days')::interval
            ORDER BY pair_address, liquidity_usd DESC
            LIMIT %s
            """,
            (min_liquidity_usd, max_liquidity_usd, str(days), max_pools),
        )
        pools = cur.fetchall()

    log.info("harvest: probing %d Solana pools", len(pools))
    if not pools:
        return []

    trade_count: Counter[str] = Counter()
    tokens_per_trader: dict[str, set[str]] = defaultdict(set)

    for i, (pool, mint, symbol, liq) in enumerate(pools, 1):
        try:
            swaps = _fetch_pool_swaps(pool, limit=swaps_per_pool)
            for tx in swaps:
                trader = tx.get("feePayer")
                if not trader or trader in SKIP_OWNERS:
                    continue
                trade_count[trader] += 1
                tokens_per_trader[trader].add(mint)
            log.info("  [%d/%d] %s liq=$%.0f swaps=%d",
                     i, len(pools), (symbol or pool[:8])[:12], liq or 0, len(swaps))
            time.sleep(0.15)
        except Exception as e:
            log.warning("  [%d/%d] %s harvest failed: %s",
                        i, len(pools), symbol or pool[:8], e)

    out = [
        (w, trade_count[w], len(tokens_per_trader[w]))
        for w in trade_count
        if len(tokens_per_trader[w]) >= min_distinct_tokens
        and min_trades <= trade_count[w] <= max_trades
    ]
    out.sort(key=lambda x: (x[2], x[1]), reverse=True)
    log.info("harvest: %d wallets after filter (%d unique total)",
             len(out), len(trade_count))
    return out
