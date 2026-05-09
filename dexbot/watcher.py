"""Watcher: poll smart-money wallets, log buy signals, open paper trades.

Cron-driven (no webhooks). Each tick:
  1. For each active scored-30+ wallet, fetch enhanced txs newer than
     last_seen_signature
  2. Parse swaps; keep BUYs from the last POLL_LOOKBACK_HOURS only
  3. INSERT signal rows (idempotent on (wallet, signature, action, token))
  4. For each NEW signal, open a paper-trade at current DexScreener price
     with fixed +18% / -12% targets
  5. Update last_seen_signature

A second pass `monitor_open_trades` polls open paper trades and closes
them on TP / SL / 24h timeout.

CLI:
    python -m dexbot.watcher                 # poll + monitor
    python -m dexbot.watcher --monitor-only  # only update open trades
    python -m dexbot.watcher --analyze       # summary report
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import db, dexscreener, helius
from .config import load_config
from .parser import parse_swaps

log = logging.getLogger("dexbot.watcher")

POLL_LOOKBACK_HOURS = 6     # ignore signals older than this on first poll
TP_PCT = 18.0
SL_PCT = -12.0
TIMEOUT_HOURS = 24
MIN_WALLET_SCORE = 30.0
MAX_TRADES_PER_POLL_PER_WALLET = 10  # cap for sanity on first run / catch-up


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def fetch_active_wallets(conn) -> list[tuple[str, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT address, last_seen_signature
            FROM watched_wallets
            WHERE is_active = TRUE AND COALESCE(score, 0) >= %s
            ORDER BY score DESC NULLS LAST
            """,
            (MIN_WALLET_SCORE,),
        )
        return cur.fetchall()


def _new_txs_since(wallet: str, last_sig: str | None, *, limit: int = 50) -> list[dict]:
    """Newest-first; cuts at last_sig (which is excluded)."""
    txs = helius.fetch_address_transactions(wallet, limit=limit)
    if not last_sig:
        return txs
    out = []
    for tx in txs:
        if tx.get("signature") == last_sig:
            break
        out.append(tx)
    return out


def _token_metadata(conn, chain: str, addr: str) -> tuple[bool | None, str | None]:
    """Returns (ever_passed, symbol) or (None, None) if unknown."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT BOOL_OR(passed_filters), MAX(symbol)
            FROM candidates
            WHERE chain = %s AND token_address = %s
            """,
            (chain, addr),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            return bool(row[0]), row[1]
        return None, None


def _insert_signal(conn, *, wallet: str, ev, ever_passed: bool | None,
                   in_candidates: bool, symbol: str | None) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wallet_signals (
                wallet, signature, block_time, action, chain, token_address,
                token_symbol, token_amount, quote_mint, quote_amount, sol_amount,
                candidate_passed, in_candidates, raw
            )
            VALUES (%s, %s, to_timestamp(%s), %s, 'solana', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (wallet, signature, action, token_address) DO NOTHING
            RETURNING id
            """,
            (
                wallet, ev.signature, ev.timestamp, ev.action, ev.token_mint, symbol,
                ev.token_amount, ev.quote_mint, ev.quote_amount, ev.sol_amount,
                ever_passed, in_candidates,
                json.dumps({"source": ev.source}),
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _fetch_pair(chain: str, token_address: str) -> dict | None:
    pairs = dexscreener.fetch_pairs_for_token(token_address)
    return dexscreener.best_pair([
        p for p in pairs if (p.get("chainId") or "").lower() == chain
    ])


def _pair_symbol(pair: dict | None) -> str | None:
    return ((pair or {}).get("baseToken") or {}).get("symbol")


def _open_paper_trade(conn, *, signal_id: int, chain: str, token_address: str,
                      symbol: str | None, wallet: str, pair: dict) -> int | None:
    """Open paper trade with fixed TP/SL given a DexScreener pair dict."""
    if not pair or not pair.get("priceUsd"):
        return None
    try:
        entry = float(pair["priceUsd"])
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    stop = entry * (1 + SL_PCT / 100.0)
    take = entry * (1 + TP_PCT / 100.0)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wallet_paper_trades (
                signal_id, chain, token_address, symbol, triggered_by_wallet,
                entry_price_usd, stop_price_usd, take_price_usd
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_id) DO NOTHING
            RETURNING id
            """,
            (signal_id, chain, token_address, symbol, wallet, entry, stop, take),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _update_wallet_cursor(conn, wallet: str, latest_sig: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE watched_wallets SET last_seen_signature=%s, last_polled_at=NOW() "
            "WHERE address=%s",
            (latest_sig, wallet),
        )


def run_once(database_url: str) -> tuple[int, int]:
    """Returns (signals_inserted, paper_trades_opened)."""
    new_signals = 0
    new_trades = 0
    cutoff = int(time.time()) - POLL_LOOKBACK_HOURS * 3600

    with db.connect(database_url) as conn:
        wallets = fetch_active_wallets(conn)
        log.info("polling %d wallets (score>=%.0f)", len(wallets), MIN_WALLET_SCORE)
        for wallet, last_sig in wallets:
            try:
                txs = _new_txs_since(wallet, last_sig, limit=50)
                if not txs:
                    log.info("  %s: no new tx", wallet[:8])
                    continue
                latest_sig_seen = txs[0].get("signature")
                events = parse_swaps(txs, wallet)
                buys = [e for e in events if e.action == "buy" and e.timestamp >= cutoff]
                buys = buys[:MAX_TRADES_PER_POLL_PER_WALLET]

                opened_this_wallet = 0
                for ev in buys:
                    ever_passed, candidate_symbol = _token_metadata(conn, "solana", ev.token_mint)
                    in_cand = ever_passed is not None
                    pair = _fetch_pair("solana", ev.token_mint)
                    symbol = candidate_symbol or _pair_symbol(pair)
                    sig_id = _insert_signal(
                        conn, wallet=wallet, ev=ev,
                        ever_passed=ever_passed, in_candidates=in_cand, symbol=symbol,
                    )
                    if sig_id is None:
                        continue
                    new_signals += 1
                    if pair is None:
                        log.warning("  no DexScreener pair for %s; signal logged but no paper trade",
                                    ev.token_mint[:8])
                        continue
                    trade_id = _open_paper_trade(
                        conn, signal_id=sig_id, chain="solana",
                        token_address=ev.token_mint, symbol=symbol, wallet=wallet, pair=pair,
                    )
                    if trade_id:
                        new_trades += 1
                        opened_this_wallet += 1
                        log.info("  trade opened: wallet=%s buy %s",
                                 wallet[:8], symbol or ev.token_mint[:8])

                if latest_sig_seen:
                    _update_wallet_cursor(conn, wallet, latest_sig_seen)
                conn.commit()
                log.info("  %s: %d new buys, %d trades opened",
                         wallet[:8], len(buys), opened_this_wallet)
            except Exception as e:
                conn.rollback()
                log.warning("error polling %s: %s", wallet[:8], e)

    return new_signals, new_trades


# ---------------------------------------------------------------------------
# Monitoring open paper trades
# ---------------------------------------------------------------------------

def monitor_open_trades(database_url: str) -> int:
    """Returns count of trades closed this pass.

    Performance: prices are fetched in batches of 30 via the DexScreener
    multi-token endpoint instead of one HTTP per trade. With ~500 open
    trades this drops monitoring from ~10 min to ~10 sec.
    """
    closed = 0
    with db.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, chain, token_address, symbol, entry_price_usd,
                       stop_price_usd, take_price_usd, opened_at
                FROM wallet_paper_trades
                WHERE status = 'open'
                ORDER BY opened_at
                """
            )
            trades = cur.fetchall()
        log.info("monitoring %d open paper trades", len(trades))
        if not trades:
            return 0

        # Group by chain to feed batch endpoint
        by_chain: dict[str, list] = {}
        for t in trades:
            by_chain.setdefault(t[1], []).append(t)

        pair_by_token: dict[str, dict] = {}
        for chain, chain_trades in by_chain.items():
            addrs = list({t[2] for t in chain_trades})
            log.info("  batch-fetching %d %s prices", len(addrs), chain)
            pairs_map = dexscreener.fetch_pairs_for_tokens(addrs)
            for addr, pair_list in pairs_map.items():
                in_chain = [
                    p for p in pair_list
                    if (p.get("chainId") or "").lower() == chain
                ]
                best = dexscreener.best_pair(in_chain)
                if best:
                    pair_by_token[addr] = best

        now = time.time()
        for trade_id, chain, addr, symbol, entry, stop, take, opened in trades:
            try:
                pair = pair_by_token.get(addr)
                if not pair or not pair.get("priceUsd"):
                    continue
                try:
                    cur_price = float(pair["priceUsd"])
                except (TypeError, ValueError):
                    continue

                age_hours = (now - opened.timestamp()) / 3600
                exit_price: float | None = None
                status: str | None = None

                if cur_price <= float(stop):
                    exit_price, status = cur_price, "closed_sl"
                elif cur_price >= float(take):
                    exit_price, status = cur_price, "closed_tp"
                elif age_hours > TIMEOUT_HOURS:
                    exit_price, status = cur_price, "closed_timeout"

                if exit_price is None:
                    continue

                pnl_pct = (exit_price - float(entry)) / float(entry) * 100.0
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wallet_paper_trades
                        SET closed_at=NOW(), exit_price_usd=%s, status=%s, pnl_pct=%s
                        WHERE id=%s
                        """,
                        (exit_price, status, pnl_pct, trade_id),
                    )
                closed += 1
                log.info("  closed #%d %s: %s pnl=%+.1f%%",
                         trade_id, symbol or addr[:8], status, pnl_pct)
            except Exception as e:
                log.warning("monitor error on #%d: %s", trade_id, e)
        conn.commit()
    return closed


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------

def analyze(database_url: str) -> None:
    with db.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN candidate_passed=TRUE  THEN 1 ELSE 0 END) AS in_passed,
                SUM(CASE WHEN candidate_passed=FALSE THEN 1 ELSE 0 END) AS in_rejected,
                SUM(CASE WHEN in_candidates=FALSE     THEN 1 ELSE 0 END) AS not_in_cand
            FROM wallet_signals WHERE action='buy'
            """
        )
        total, p, r, ni = cur.fetchone()
        print("\n=== Wallet BUY signals ===")
        print(f"  total signals                    : {total}")
        print(f"  + token passed screener filters  : {p}")
        print(f"  + token in screener but rejected : {r}")
        print(f"  + token not in screener at all   : {ni}")

        cur.execute(
            """
            SELECT status, COUNT(*),
                   ROUND(AVG(pnl_pct)::numeric, 1) AS avg_pnl,
                   ROUND(MIN(pnl_pct)::numeric, 1) AS worst,
                   ROUND(MAX(pnl_pct)::numeric, 1) AS best
            FROM wallet_paper_trades
            GROUP BY status ORDER BY COUNT(*) DESC
            """
        )
        rows = cur.fetchall()
        print("\n=== Wallet-driven paper trades ===")
        if not rows:
            print("  (none yet)")
            return
        print(f"  {'status':<18s} {'n':>4s} {'avg_pnl':>9s} {'worst':>8s} {'best':>8s}")
        total_n = 0
        wins = 0
        losses = 0
        weighted_pnl = 0.0
        for status, n, avg, worst, best in rows:
            avg_s = f"{float(avg):+.1f}%" if avg is not None else "n/a"
            wo_s = f"{float(worst):+.1f}%" if worst is not None else "n/a"
            be_s = f"{float(best):+.1f}%" if best is not None else "n/a"
            print(f"  {status:<18s} {n:>4d} {avg_s:>9s} {wo_s:>8s} {be_s:>8s}")
            total_n += n
            if status == "closed_tp":
                wins = n
            if status == "closed_sl":
                losses = n
            if avg is not None:
                weighted_pnl += float(avg) * n
        decided = wins + losses
        if decided:
            wr = wins / decided * 100
            print(f"\n  win rate (TP / SL decided)      : {wr:.0f}%  ({wins}W / {losses}L)")
        if total_n:
            print(f"  avg pnl across all trades       : {weighted_pnl / total_n:+.2f}%")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dexbot.watcher")
    p.add_argument("--monitor-only", action="store_true",
                   help="Skip polling; only check open paper trades.")
    p.add_argument("--analyze", action="store_true",
                   help="Print summary; don't poll or modify.")
    args = p.parse_args(argv)

    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    if args.analyze:
        analyze(config.database_url)
        return 0

    if not args.monitor_only:
        s, t = run_once(config.database_url)
        print(f"watcher: {s} new signals, {t} paper trades opened")
    closed = monitor_open_trades(config.database_url)
    print(f"monitor: {closed} trades closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
