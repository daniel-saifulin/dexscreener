"""Shadow paper-trader.

Every cron tick: find tokens detected in the last 25h that haven't been
probed in the last 25 min, hit DexScreener for current price, store the
percent change vs the first-seen price.

After 24-72 hours of accumulation, `--analyze` produces the answer to the
real question: of tokens that PASSED our screener filters, what fraction
hit +18% before -12%? And what's the rejected-group baseline?

CLI:
    python -m dexbot.probes              # one cycle
    python -m dexbot.probes --analyze    # SQL summary report
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from . import db, dexscreener
from .config import load_config

log = logging.getLogger("dexbot.probes")

PROBE_HORIZON_HOURS = 24
COOLDOWN_MIN = 25  # don't reprobe a token sooner than this


def fetch_targets(conn) -> list[dict]:
    """Tokens with first detection within the horizon, not probed recently."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.chain, c.token_address,
                   MIN(c.detected_at)                                   AS first_detected_at,
                   (ARRAY_AGG(c.price_usd ORDER BY c.detected_at ASC))[1] AS first_price,
                   BOOL_OR(c.passed_filters)                            AS ever_passed
            FROM candidates c
            WHERE c.detected_at >= NOW() - INTERVAL '{PROBE_HORIZON_HOURS + 1} hours'
              AND c.price_usd IS NOT NULL
              AND c.price_usd > 0
              AND NOT EXISTS (
                SELECT 1 FROM candidate_probes p
                WHERE p.chain = c.chain
                  AND p.token_address = c.token_address
                  AND p.probed_at > NOW() - INTERVAL '{COOLDOWN_MIN} minutes'
              )
            GROUP BY c.chain, c.token_address
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def insert_probe(
    conn,
    *,
    chain: str,
    token_address: str,
    age_minutes: int,
    price_usd: float,
    pct_change: float,
    passed_filters: bool,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO candidate_probes
              (chain, token_address, age_minutes, price_usd, pct_change, passed_filters)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (chain, token_address, age_minutes, price_usd, pct_change, passed_filters),
        )


def run_once(database_url: str) -> tuple[int, int]:
    """Returns (probed_ok, probed_skipped)."""
    now = time.time()
    ok = 0
    skipped = 0
    with db.connect(database_url) as conn:
        targets = fetch_targets(conn)
        log.info("found %d tokens needing probes", len(targets))
        for t in targets:
            age_sec = now - t["first_detected_at"].timestamp()
            age_min = int(age_sec / 60)
            if age_min > PROBE_HORIZON_HOURS * 60:
                continue  # past horizon
            try:
                pairs = dexscreener.fetch_pairs_for_token(t["token_address"])
                pair = dexscreener.best_pair([
                    p for p in pairs if (p.get("chainId") or "").lower() == t["chain"]
                ])
                if not pair or not pair.get("priceUsd"):
                    skipped += 1
                    continue
                cur_price = float(pair["priceUsd"])
                detect_price = float(t["first_price"])
                if detect_price <= 0:
                    skipped += 1
                    continue
                pct = (cur_price - detect_price) / detect_price * 100.0
                insert_probe(
                    conn,
                    chain=t["chain"],
                    token_address=t["token_address"],
                    age_minutes=age_min,
                    price_usd=cur_price,
                    pct_change=pct,
                    passed_filters=bool(t["ever_passed"]),
                )
                ok += 1
            except Exception as e:
                log.warning("probe %s failed: %s", t["token_address"][:8], e)
                skipped += 1
        conn.commit()
    return ok, skipped


def analyze(database_url: str) -> None:
    """Per-token aggregates -> grouped summary by passed_filters."""
    with db.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH per_token AS (
              SELECT chain, token_address,
                     BOOL_OR(passed_filters)         AS passed_filters,
                     MAX(pct_change)                 AS max_pct,
                     MIN(pct_change)                 AS min_pct,
                     MAX(age_minutes)                AS last_age,
                     COUNT(*)                        AS n_probes
              FROM candidate_probes
              GROUP BY chain, token_address
            )
            SELECT
              passed_filters,
              COUNT(*)                                              AS tokens,
              ROUND(AVG(max_pct)::numeric, 1)                       AS avg_peak,
              ROUND(AVG(min_pct)::numeric, 1)                       AS avg_trough,
              SUM(CASE WHEN max_pct >= 18 THEN 1 ELSE 0 END)        AS hit_tp_18,
              SUM(CASE WHEN max_pct >= 50 THEN 1 ELSE 0 END)        AS hit_50,
              SUM(CASE WHEN min_pct <= -12 THEN 1 ELSE 0 END)       AS hit_sl_12,
              SUM(CASE WHEN min_pct <= -50 THEN 1 ELSE 0 END)       AS rugged_50,
              ROUND(AVG(n_probes)::numeric, 1)                      AS avg_probes,
              ROUND(AVG(last_age)::numeric, 0)                      AS avg_last_age_min
            FROM per_token
            GROUP BY passed_filters
            ORDER BY passed_filters DESC
            """
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*), MIN(probed_at), MAX(probed_at) FROM candidate_probes")
        n_probes, first_p, last_p = cur.fetchone()

    print(f"\n=== Shadow paper-trader summary ===")
    print(f"  total probes recorded : {n_probes}")
    if first_p:
        print(f"  observation window    : {first_p} → {last_p}")
    print()
    if not rows:
        print("  (no probe data yet; let cron run for a few hours)")
        return
    print(f"{'group':<10s} {'tokens':>7s} {'avg_peak':>9s} {'avg_trough':>10s} "
          f"{'hit+18':>7s} {'hit+50':>7s} {'hit-12':>7s} {'rug-50':>7s} "
          f"{'probes':>7s} {'lastAgeM':>9s}")
    for passed, tokens, peak, trough, tp, p50, sl, rug, np_, lag in rows:
        label = "passed" if passed else "rejected"
        peak = float(peak or 0)
        trough = float(trough or 0)
        np_ = float(np_ or 0)
        lag = float(lag or 0)
        print(f"{label:<10s} {tokens:>7d} {peak:>+8.1f}% {trough:>+9.1f}% "
              f"{tp:>7d} {p50:>7d} {sl:>7d} {rug:>7d} "
              f"{np_:>7.1f} {lag:>9.0f}")
    print()
    print("  Read this as: of N tokens in each group, how many EVER hit each level "
          "during their\n  24h observation window. The hit-+18 column is your"
          " realistic upside hit-rate.\n  Compare passed vs rejected to see if"
          " the screener filters add information.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dexbot.probes")
    p.add_argument("--analyze", action="store_true",
                   help="Print SQL summary instead of probing.")
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

    ok, skipped = run_once(config.database_url)
    print(f"probed: {ok} ok, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
