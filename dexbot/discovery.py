"""Discovery: keep `watched_wallets` populated and scored.

CLI:
    python -m dexbot.discovery add ADDR [--source NAME] [--note TEXT]
    python -m dexbot.discovery list [--limit 20]
    python -m dexbot.discovery score [--wallet ADDR | --all] [--days 30]
    python -m dexbot.discovery remove ADDR
    python -m dexbot.discovery harvest [--max-pools 30] [--days 14]
    python -m dexbot.discovery promote ADDR    # включить в core-набор
    python -m dexbot.discovery demote ADDR     # исключить из core-набора
    python -m dexbot.discovery list-core       # показать текущее ядро

Scoring is intentionally simple for v1: it ranks wallets by a composite of
30-day trade count, distinct tokens traded, recency, and SOL-flow PnL
(realised SOL out − SOL in across the window). We do NOT compute exact
USD PnL — that requires per-token historical prices and is a follow-up.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass

from . import db, harvest, helius
from .config import Config, load_config
from .parser import SOL_MINT, SwapEvent, parse_swaps

log = logging.getLogger("dexbot.discovery")

DEFAULT_WINDOW_DAYS = 30


@dataclass
class WalletStats:
    address: str
    trades: int
    distinct_tokens: int
    buys: int
    sells: int
    buy_volume_sol: float
    sell_volume_sol: float
    realized_pnl_sol: float
    last_active_ts: int | None
    score: float


def _score(stats: WalletStats, *, now_ts: int) -> float:
    """Higher = better. Calibrated to put healthy active memecoin traders
    in the 30-100 range; bots and dead wallets near 0.

    Components:
      - log-scaled trade count (saturates around 100)
      - distinct-tokens diversity (caps at 25)
      - PnL bonus (linear in realized SOL, capped to avoid whale dominance)
      - recency multiplier (decays to 0.3 if last active >7d ago)
    """
    import math

    if stats.trades < 5:
        return 0.0

    trade_term = min(40.0, 10.0 * math.log10(stats.trades + 1))
    diversity_term = min(20.0, stats.distinct_tokens * 0.8)
    pnl_term = max(-10.0, min(40.0, stats.realized_pnl_sol * 0.5))

    if stats.last_active_ts is None:
        recency_mult = 0.3
    else:
        age_days = max(0.0, (now_ts - stats.last_active_ts) / 86_400)
        if age_days <= 1:
            recency_mult = 1.0
        elif age_days >= 7:
            recency_mult = 0.3
        else:
            recency_mult = 1.0 - (age_days - 1) / 6 * 0.7

    return (trade_term + diversity_term + pnl_term) * recency_mult


def aggregate_stats(events: list[SwapEvent], wallet: str) -> WalletStats:
    if not events:
        return WalletStats(
            address=wallet, trades=0, distinct_tokens=0, buys=0, sells=0,
            buy_volume_sol=0, sell_volume_sol=0, realized_pnl_sol=0,
            last_active_ts=None, score=0.0,
        )

    distinct = {ev.token_mint for ev in events}
    buys = [ev for ev in events if ev.action == "buy"]
    sells = [ev for ev in events if ev.action == "sell"]

    # SOL-flow proxy for PnL. Quote_amount in USDC/USDT is treated as SOL-equivalent
    # at a flat 1-USDC≈0.005-SOL rate to avoid pulling price history (rough but stable).
    def to_sol(ev: SwapEvent) -> float:
        if ev.sol_amount is not None:
            return ev.sol_amount
        # Rough fallback — SOL price between $20 and $300 swings, but for
        # ranking purposes we just need a consistent number across wallets.
        return ev.quote_amount * 0.005

    buy_sol = sum(to_sol(ev) for ev in buys)
    sell_sol = sum(to_sol(ev) for ev in sells)
    last_ts = max((ev.timestamp for ev in events), default=None)

    stats = WalletStats(
        address=wallet,
        trades=len(events),
        distinct_tokens=len(distinct),
        buys=len(buys),
        sells=len(sells),
        buy_volume_sol=buy_sol,
        sell_volume_sol=sell_sol,
        realized_pnl_sol=sell_sol - buy_sol,
        last_active_ts=last_ts,
        score=0.0,
    )
    stats.score = _score(stats, now_ts=int(time.time()))
    return stats


# ---------------------------------------------------------------------------
# DB ops
# ---------------------------------------------------------------------------

def add_wallet(conn, address: str, source: str = "manual", notes: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watched_wallets (address, source, notes, is_active)
            VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (address) DO UPDATE
              SET source = COALESCE(EXCLUDED.source, watched_wallets.source),
                  notes  = COALESCE(EXCLUDED.notes, watched_wallets.notes),
                  is_active = TRUE
            """,
            (address, source, notes),
        )


def remove_wallet(conn, address: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE watched_wallets SET is_active = FALSE WHERE address = %s", (address,))


def list_wallets(conn, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT address, source, score, trades_30d, distinct_tokens_30d,
                   realized_pnl_30d_sol, last_active_at, last_scored_at
            FROM watched_wallets
            WHERE is_active
            ORDER BY score DESC NULLS LAST, last_scored_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_active_addresses(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT address FROM watched_wallets WHERE is_active ORDER BY added_at")
        return [r[0] for r in cur.fetchall()]


def fetch_addresses_to_score(conn, *, stale_after_hours: int = 168,
                             max_count: int | None = None) -> list[str]:
    """Возвращает адреса для score, приоритизированные:
    1. Никогда не оценивались (last_scored_at IS NULL) → первый эшелон
    2. Устаревшие (last_scored_at старше stale_after_hours, по умолчанию 7 дней)
    3. Внутри каждого эшелона — самые старые last_scored_at сначала

    Это даёт стабильное время выполнения score-all при растущем watch-листе.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT address
            FROM watched_wallets
            WHERE is_active = TRUE
              AND (
                last_scored_at IS NULL
                OR last_scored_at < NOW() - INTERVAL '{stale_after_hours} hours'
              )
            ORDER BY (last_scored_at IS NULL) DESC, last_scored_at ASC NULLS FIRST
            {"LIMIT " + str(int(max_count)) if max_count else ""}
            """,
        )
        return [r[0] for r in cur.fetchall()]


def upsert_score(conn, stats: WalletStats) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE watched_wallets SET
              last_scored_at       = NOW(),
              score                = %s,
              trades_30d           = %s,
              distinct_tokens_30d  = %s,
              buy_volume_30d_sol   = %s,
              sell_volume_30d_sol  = %s,
              realized_pnl_30d_sol = %s,
              last_active_at       = to_timestamp(%s)
            WHERE address = %s
            """,
            (
                stats.score, stats.trades, stats.distinct_tokens,
                stats.buy_volume_sol, stats.sell_volume_sol,
                stats.realized_pnl_sol,
                stats.last_active_ts,
                stats.address,
            ),
        )


def insert_activity(conn, events: list[SwapEvent]) -> int:
    """Append parsed swap events; ON CONFLICT skips dups."""
    if not events:
        return 0
    inserted = 0
    with conn.cursor() as cur:
        for ev in events:
            cur.execute(
                """
                INSERT INTO wallet_activity (
                    wallet, signature, block_time, action,
                    token_address, token_amount, sol_amount, source, raw
                )
                VALUES (%s, %s, to_timestamp(%s), %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (wallet, signature, action, token_address) DO NOTHING
                """,
                (
                    ev.wallet, ev.signature, ev.timestamp, ev.action,
                    ev.token_mint, ev.token_amount, ev.sol_amount,
                    ev.source,
                    json.dumps({"quote_mint": ev.quote_mint, "quote_amount": ev.quote_amount}),
                ),
            )
            inserted += cur.rowcount
    return inserted


# ---------------------------------------------------------------------------
# Score one wallet
# ---------------------------------------------------------------------------

def score_wallet(conn, address: str, *, days: int = DEFAULT_WINDOW_DAYS) -> WalletStats:
    since = int(time.time()) - days * 86_400
    log.info("fetching tx for %s since %s", address[:8], since)
    txs = helius.fetch_transactions_since(address, since_unix=since)
    log.info("  %d transactions", len(txs))

    events = parse_swaps(txs, address)
    log.info("  %d parsed swaps", len(events))

    stats = aggregate_stats(events, address)
    insert_activity(conn, events)
    upsert_score(conn, stats)
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_wallets(rows: list[dict]) -> None:
    if not rows:
        print("(no wallets)")
        return
    print(f"{'address':<48s} {'score':>7s} {'trades':>7s} {'tokens':>7s} "
          f"{'pnl_sol':>9s} {'last_active':<20s}")
    for r in rows:
        addr = r["address"]
        score = r["score"] or 0
        trades = r["trades_30d"] or 0
        tokens = r["distinct_tokens_30d"] or 0
        pnl = r["realized_pnl_30d_sol"] or 0
        last = r["last_active_at"].strftime("%Y-%m-%d %H:%M") if r["last_active_at"] else "-"
        print(f"{addr:<48s} {score:>7.1f} {trades:>7d} {tokens:>7d} "
              f"{pnl:>+9.2f} {last:<20s}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dexbot.discovery")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Add a wallet to the watchlist.")
    p_add.add_argument("address")
    p_add.add_argument("--source", default="manual")
    p_add.add_argument("--note")

    p_rm = sub.add_parser("remove", help="Mark a wallet inactive.")
    p_rm.add_argument("address")

    p_list = sub.add_parser("list", help="List active wallets ordered by score.")
    p_list.add_argument("--limit", type=int, default=20)

    p_sc = sub.add_parser("score", help="Re-fetch and re-score wallet(s).")
    p_sc_grp = p_sc.add_mutually_exclusive_group(required=True)
    p_sc_grp.add_argument("--wallet", help="Score a single wallet.")
    p_sc_grp.add_argument("--all", action="store_true", help="Score all active wallets.")
    p_sc.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    p_sc.add_argument("--stale-only", action="store_true",
                      help="Only score wallets unscored or older than --stale-after-hours.")
    p_sc.add_argument("--stale-after-hours", type=int, default=168,
                      help="Re-score threshold for --stale-only (default: 168h = 7d).")
    p_sc.add_argument("--max", type=int, default=None,
                      help="Cap number of wallets scored in one run.")

    p_h = sub.add_parser(
        "harvest",
        help="Find new candidate wallets from on-chain pool activity.",
    )
    p_h.add_argument("--max-pools", type=int, default=30)
    p_h.add_argument("--days", type=int, default=14,
                     help="Only use candidates detected in the last N days.")
    p_h.add_argument("--min-distinct-tokens", type=int, default=2)
    p_h.add_argument("--min-trades", type=int, default=5)
    p_h.add_argument("--max-trades", type=int, default=200)

    p_pr = sub.add_parser("promote", help="Add wallet to core set (will trigger paper trades).")
    p_pr.add_argument("address")

    p_dm = sub.add_parser("demote", help="Remove wallet from core set (signals still logged).")
    p_dm.add_argument("address")

    sub.add_parser("list-core", help="Show wallets currently in core set.")

    args = p.parse_args(argv)
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not config.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    with db.connect(config.database_url) as conn:
        if args.cmd == "add":
            add_wallet(conn, args.address, source=args.source, notes=args.note)
            conn.commit()
            print(f"added {args.address}")
            return 0

        if args.cmd == "remove":
            remove_wallet(conn, args.address)
            conn.commit()
            print(f"deactivated {args.address}")
            return 0

        if args.cmd == "list":
            _print_wallets(list_wallets(conn, args.limit))
            return 0

        if args.cmd == "score":
            if args.wallet:
                addresses = [args.wallet]
            elif args.stale_only:
                addresses = fetch_addresses_to_score(
                    conn,
                    stale_after_hours=args.stale_after_hours,
                    max_count=args.max,
                )
            else:
                addresses = fetch_active_addresses(conn)
                if args.max is not None:
                    addresses = addresses[:args.max]
            if not addresses:
                print("no wallets need scoring — все актуальны или watch-список пуст")
                return 0
            mode = "stale-only" if args.stale_only else "all"
            print(f"scoring {len(addresses)} wallet(s) over {args.days}d window  [mode={mode}]")
            for addr in addresses:
                try:
                    stats = score_wallet(conn, addr, days=args.days)
                    print(f"  {addr[:8]}…  score={stats.score:>6.1f}  "
                          f"trades={stats.trades:>3d}  tokens={stats.distinct_tokens:>3d}  "
                          f"pnl_sol={stats.realized_pnl_sol:+7.2f}")
                except helius.HeliusError as e:
                    log.warning("skipping %s: %s", addr, e)
            return 0

        if args.cmd == "harvest":
            results = harvest.harvest_from_pools(
                conn,
                days=args.days,
                max_pools=args.max_pools,
                min_distinct_tokens=args.min_distinct_tokens,
                min_trades=args.min_trades,
                max_trades=args.max_trades,
            )
            added = 0
            for wallet, trades, tokens in results:
                add_wallet(
                    conn, wallet, source="harvest_pool",
                    notes=f"{tokens} mints / {trades} trades observed at harvest",
                )
                added += 1
            conn.commit()
            print(f"harvested {len(results)} candidate(s); upserted into watchlist: {added}")
            for w, t, n in results[:30]:
                print(f"  {w}  tokens={n:>2d}  trades={t:>3d}")
            return 0

        if args.cmd == "promote":
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE watched_wallets SET is_core=TRUE, core_set_at=NOW() "
                    "WHERE address=%s",
                    (args.address,),
                )
                ok = cur.rowcount
            conn.commit()
            print(f"promoted to core: {args.address}" if ok else f"wallet not found: {args.address}")
            return 0

        if args.cmd == "demote":
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE watched_wallets SET is_core=FALSE WHERE address=%s",
                    (args.address,),
                )
                ok = cur.rowcount
            conn.commit()
            print(f"demoted from core: {args.address}" if ok else f"wallet not found: {args.address}")
            return 0

        if args.cmd == "list-core":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT address, score, trades_30d, distinct_tokens_30d,
                           last_active_at, core_set_at
                    FROM watched_wallets
                    WHERE is_core=TRUE AND is_active=TRUE
                    ORDER BY score DESC NULLS LAST
                    """
                )
                rows = cur.fetchall()
            if not rows:
                print("(core set is empty)")
                return 0
            print(f"{'address':<48s} {'score':>7s} {'trades30d':>10s} "
                  f"{'tokens30d':>10s} {'core_since':<20s}")
            for addr, score, trades, tokens, last, core_at in rows:
                core_at_s = core_at.strftime("%Y-%m-%d %H:%M") if core_at else "-"
                print(f"{addr:<48s} {float(score or 0):>7.1f} "
                      f"{trades or 0:>10d} {tokens or 0:>10d} {core_at_s:<20s}")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
