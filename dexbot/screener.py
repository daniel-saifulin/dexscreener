"""Single-pass screener: pull candidates, apply filters, persist or print.

CLI:
    python -m dexbot.screener           # full pass with DB persistence
    python -m dexbot.screener --dry-run # no DB; prints to stdout
    python -m dexbot.screener --migrate # apply migrations and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import db
from .config import Config, load_config
from .dexscreener import (
    best_pair,
    fetch_latest_boosts,
    fetch_latest_profiles,
    fetch_pairs_for_token,
)
from .filters import evaluate_pair, evaluate_safety
from .safety import check as safety_check

log = logging.getLogger("dexbot.screener")


def _gather_candidates() -> list[tuple[str, str]]:
    """Returns deduped (chainId, tokenAddress) pairs from profiles + boosts."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for src in (fetch_latest_profiles(), fetch_latest_boosts()):
        for entry in src:
            chain = (entry.get("chainId") or "").lower()
            addr = entry.get("tokenAddress")
            if not chain or not addr:
                continue
            key = (chain, addr)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _enrich_and_filter(
    candidate_keys: list[tuple[str, str]],
    config: Config,
) -> list[dict]:
    """For each (chain, address): fetch pair, run filter, then safety on passing."""
    now_ms = int(time.time() * 1000)
    results: list[dict] = []

    for chain, addr in candidate_keys:
        if chain not in config.chains:
            continue
        try:
            pairs = fetch_pairs_for_token(addr)
        except Exception as e:
            log.warning("pair fetch failed for %s/%s: %s", chain, addr, e)
            continue

        # Filter to the requested chain only — token may be cross-listed.
        pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain]
        pair = best_pair(pairs)
        if not pair:
            continue

        result = evaluate_pair(pair, config, now_ms=now_ms)
        safety: dict = {}

        if result.passed:
            try:
                safety = safety_check(chain, addr)
            except Exception as e:
                log.warning("safety fetch failed for %s/%s: %s", chain, addr, e)
                safety = {"flags": [], "source": "error"}
            safety_reasons = evaluate_safety(safety, config)
            if safety_reasons:
                result.passed = False
                result.reasons.extend(f"safety:{r}" for r in safety_reasons)

        results.append({
            "chain": chain,
            "address": addr,
            "pair": pair,
            "safety": safety,
            "passed": result.passed,
            "reasons": result.reasons,
        })

    return results


def _print_summary(results: list[dict]) -> None:
    passed = [r for r in results if r["passed"]]
    print(f"\n=== Screener pass ===")
    print(f"  candidates evaluated : {len(results)}")
    print(f"  passed filters       : {len(passed)}")
    if passed:
        print()
        for r in passed:
            p = r["pair"]
            sym = (p.get("baseToken") or {}).get("symbol") or "?"
            liq = (p.get("liquidity") or {}).get("usd") or 0
            chg = (p.get("priceChange") or {}).get("h1")
            vol = (p.get("volume") or {}).get("h1") or 0
            url = p.get("url") or ""
            chg_s = f"{chg:+.1f}%" if chg is not None else "n/a"
            print(f"  ✓ {sym:>10s} on {r['chain']:<10s} liq=${liq:>9,.0f} "
                  f"vol1h=${vol:>8,.0f} chg1h={chg_s:>7s}  {url}")
    print()


def run_once(config: Config, *, dry_run: bool) -> int:
    log.info("collecting candidate keys from profiles + boosts")
    keys = _gather_candidates()
    log.info("collected %d unique tokens", len(keys))

    results = _enrich_and_filter(keys, config)
    _print_summary(results)

    if dry_run or not config.database_url:
        if not dry_run and not config.database_url:
            log.warning("DATABASE_URL not set; skipping persistence")
        return 0

    with db.connect(config.database_url) as conn:
        for r in results:
            p = r["pair"]
            base = p.get("baseToken") or {}
            txns_h1 = (p.get("txns") or {}).get("h1") or {}
            created_at = p.get("pairCreatedAt")
            age_min = None
            if created_at:
                try:
                    age_min = int((time.time() * 1000 - int(created_at)) / 60_000)
                except (TypeError, ValueError):
                    age_min = None
            try:
                price = float(p.get("priceUsd")) if p.get("priceUsd") else None
            except (TypeError, ValueError):
                price = None

            db.insert_candidate(
                conn,
                chain=r["chain"],
                token_address=r["address"],
                pair_address=p.get("pairAddress") or "",
                symbol=base.get("symbol"),
                name=base.get("name"),
                price_usd=price,
                liquidity_usd=(p.get("liquidity") or {}).get("usd"),
                volume_h1_usd=(p.get("volume") or {}).get("h1"),
                price_change_h1=(p.get("priceChange") or {}).get("h1"),
                pair_age_minutes=age_min,
                buys_h1=txns_h1.get("buys"),
                sells_h1=txns_h1.get("sells"),
                passed_filters=r["passed"],
                filter_reasons=r["reasons"],
                safety_flags=r["safety"],
                raw=p,
            )
            if r["passed"] and r["safety"]:
                db.upsert_safety_cache(conn, r["chain"], r["address"], r["safety"])
        conn.commit()
        log.info("persisted %d rows to candidates", len(results))

    # Гипотеза C: после записи candidates — открываем screener-only paper-сделки
    # для свежих passed-кандидатов. Независимая когорта, отдельная таблица.
    try:
        from . import screener_trader
        opened, skipped = screener_trader.open_screener_trades(config.database_url)
        log.info("screener-trader: opened=%d skipped=%d", opened, skipped)
        n_closed = screener_trader.monitor_screener_trades(config.database_url)
        log.info("screener-trader monitor: closed=%d", n_closed)
    except Exception as e:
        log.warning("screener_trader pass failed: %s", e)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one screener pass.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DB writes; print to stdout only.")
    parser.add_argument("--migrate", action="store_true",
                        help="Apply SQL migrations and exit.")
    args = parser.parse_args(argv)

    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.migrate:
        if not config.database_url:
            print("ERROR: DATABASE_URL required for --migrate", file=sys.stderr)
            return 2
        db.run_migrations(config.database_url)
        print("migrations applied")
        return 0

    return run_once(config, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
