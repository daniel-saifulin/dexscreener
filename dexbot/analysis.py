"""Periodic comprehensive analysis report.

Reads from Postgres, prints either plain-text or GitHub-flavored markdown.
The markdown form is meant to be appended to $GITHUB_STEP_SUMMARY in the
analysis workflow, so the report renders inline on the run page.

CLI:
    python -m dexbot.analysis              # human-readable to stdout
    python -m dexbot.analysis --markdown   # GH markdown to stdout
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from . import db
from .config import load_config

log = logging.getLogger("dexbot.analysis")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pct(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _md_table(headers: list[str], rows: list[list]) -> str:
    out = "| " + " | ".join(headers) + " |\n"
    out += "| " + " | ".join("---" for _ in headers) + " |\n"
    for row in rows:
        out += "| " + " | ".join(str(c) for c in row) + " |\n"
    return out


def _txt_table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "  (empty)\n"
    widths = [
        max(len(str(headers[i])),
            max((len(str(row[i])) for row in rows), default=0))
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return (fmt.format(*headers) + "\n"
            + "\n".join(fmt.format(*[str(c) for c in r]) for r in rows) + "\n")


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

def fetch_activity(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              (SELECT COUNT(DISTINCT token_address) FROM candidates
                  WHERE detected_at >= NOW() - INTERVAL '24 hours'),
              (SELECT COUNT(DISTINCT token_address) FROM candidates
                  WHERE detected_at >= NOW() - INTERVAL '24 hours' AND passed_filters = TRUE),
              (SELECT COUNT(*) FROM wallet_signals
                  WHERE detected_at >= NOW() - INTERVAL '24 hours' AND action='buy'),
              (SELECT COUNT(*) FROM candidate_probes
                  WHERE probed_at >= NOW() - INTERVAL '24 hours'),
              (SELECT COUNT(*) FROM wallet_paper_trades),
              (SELECT COUNT(*) FROM wallet_paper_trades WHERE status='open'),
              (SELECT COUNT(*) FROM watched_wallets WHERE is_active=TRUE)
        """)
        return cur.fetchone()


def fetch_watcher_summary(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status, COUNT(*),
                   ROUND(AVG(pnl_pct)::numeric, 1),
                   ROUND(MIN(pnl_pct)::numeric, 1),
                   ROUND(MAX(pnl_pct)::numeric, 1)
            FROM wallet_paper_trades
            GROUP BY status
            ORDER BY COUNT(*) DESC
        """)
        return cur.fetchall()


def fetch_probes_outcomes(conn):
    """Simulates +18/-12 paper trade per detected token, grouped by passed/rejected."""
    with conn.cursor() as cur:
        cur.execute("""
          WITH first_tp AS (
            SELECT chain, token_address, MIN(probed_at) AS at_ FROM candidate_probes
            WHERE pct_change >= 18 GROUP BY chain, token_address
          ),
          first_sl AS (
            SELECT chain, token_address, MIN(probed_at) AS at_ FROM candidate_probes
            WHERE pct_change <= -12 GROUP BY chain, token_address
          ),
          ever_passed AS (
            SELECT chain, token_address, BOOL_OR(passed_filters) AS passed
            FROM candidates GROUP BY chain, token_address
          ),
          per_token AS (
            SELECT cp.chain, cp.token_address, ep.passed,
                   MAX(cp.pct_change) AS peak,
                   MIN(cp.pct_change) AS trough,
                   CASE
                     WHEN tp.at_ IS NOT NULL AND sl.at_ IS NOT NULL THEN
                       CASE WHEN tp.at_ < sl.at_ THEN 'win' ELSE 'loss' END
                     WHEN tp.at_ IS NOT NULL THEN 'win'
                     WHEN sl.at_ IS NOT NULL THEN 'loss'
                     ELSE 'open'
                   END AS outcome
            FROM candidate_probes cp
            JOIN ever_passed ep USING (chain, token_address)
            LEFT JOIN first_tp tp USING (chain, token_address)
            LEFT JOIN first_sl sl USING (chain, token_address)
            GROUP BY cp.chain, cp.token_address, ep.passed, tp.at_, sl.at_
          )
          SELECT passed,
                 COUNT(*) AS tokens,
                 SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                 SUM(CASE WHEN outcome='open' THEN 1 ELSE 0 END) AS opens,
                 ROUND(AVG(peak)::numeric, 1)   AS avg_peak,
                 ROUND(AVG(trough)::numeric, 1) AS avg_trough
          FROM per_token GROUP BY passed ORDER BY passed DESC NULLS LAST
        """)
        return cur.fetchall()


def fetch_cross_wallet_tokens(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.token_address, COALESCE(s.token_symbol, '?') AS sym,
                   COUNT(DISTINCT s.wallet) AS n_wallets,
                   COUNT(*) AS n_signals,
                   COUNT(t.id) FILTER (WHERE t.status='open')      AS open_trades,
                   COUNT(t.id) FILTER (WHERE t.status='closed_tp') AS wins,
                   COUNT(t.id) FILTER (WHERE t.status='closed_sl') AS losses,
                   ROUND(AVG(t.pnl_pct)::numeric, 1) AS avg_pnl
            FROM wallet_signals s
            LEFT JOIN wallet_paper_trades t ON t.signal_id = s.id
            WHERE s.action='buy'
              AND s.detected_at >= NOW() - INTERVAL '48 hours'
            GROUP BY s.token_address, s.token_symbol
            HAVING COUNT(DISTINCT s.wallet) >= 2
            ORDER BY n_wallets DESC, n_signals DESC
            LIMIT 15
        """)
        return cur.fetchall()


def fetch_conviction_comparison(conn):
    """Cross-wallet (≥2) vs single-wallet trades — outcome split."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH conviction AS (
              SELECT chain, token_address,
                     COUNT(DISTINCT wallet) AS n_wallets
              FROM wallet_signals WHERE action='buy'
              GROUP BY chain, token_address
            )
            SELECT
              CASE WHEN c.n_wallets >= 2 THEN 'cross_wallet (>=2)' ELSE 'single_wallet' END AS cohort,
              COUNT(t.id) AS trades,
              SUM(CASE WHEN t.status='closed_tp' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN t.status='closed_sl' THEN 1 ELSE 0 END) AS losses,
              SUM(CASE WHEN t.status LIKE 'closed%%' THEN 1 ELSE 0 END) AS closed_total,
              ROUND(AVG(t.pnl_pct)::numeric, 1) AS avg_pnl
            FROM wallet_paper_trades t
            JOIN conviction c USING (chain, token_address)
            GROUP BY 1 ORDER BY 1
        """)
        return cur.fetchall()


def fetch_wallet_cohorts(conn):
    """Per-wallet aggregate trade outcomes."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.triggered_by_wallet AS wallet,
                   w.score,
                   w.is_core,
                   ROUND(EXTRACT(EPOCH FROM (NOW() - w.added_at))/3600, 1) AS age_hours,
                   COUNT(t.id) AS trades,
                   SUM(CASE WHEN t.status='closed_tp' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN t.status='closed_sl' THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN t.status LIKE 'closed%%' THEN 1 ELSE 0 END) AS closed,
                   ROUND(AVG(t.pnl_pct)::numeric, 1) AS avg_pnl,
                   ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.pnl_pct)
                         FILTER (WHERE t.status LIKE 'closed%%')::numeric, 1) AS median_pnl
            FROM wallet_paper_trades t
            JOIN watched_wallets w ON w.address = t.triggered_by_wallet
            GROUP BY t.triggered_by_wallet, w.score, w.is_core, w.added_at
            ORDER BY trades DESC
            LIMIT 20
        """)
        return cur.fetchall()


def fetch_core_strategy_outcomes(conn):
    """Performance of core-conviction trades only (Уровень 2)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                status, COUNT(*),
                ROUND(AVG(pnl_pct)::numeric, 1),
                ROUND(MIN(pnl_pct)::numeric, 1),
                ROUND(MAX(pnl_pct)::numeric, 1),
                ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 1)
            FROM wallet_paper_trades
            WHERE from_core_conviction = TRUE
            GROUP BY status ORDER BY COUNT(*) DESC
        """)
        return cur.fetchall()


def fetch_latency_experiment(conn):
    """Latency experiment: webhook (fly.io, ~10s) vs cron (GH Actions, ~10min)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                CASE WHEN from_webhook THEN 'webhook (low-latency)' ELSE 'cron (10-min)' END AS source,
                COUNT(*) AS trades,
                COUNT(*) FILTER (WHERE status = 'open') AS open_,
                COUNT(*) FILTER (WHERE status = 'closed_tp') AS wins,
                COUNT(*) FILTER (WHERE status = 'closed_sl') AS losses,
                COUNT(*) FILTER (WHERE status = 'closed_timeout') AS timeouts,
                ROUND(AVG(pnl_pct) FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS mean_pnl,
                ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)
                      FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS median_pnl
            FROM wallet_paper_trades
            WHERE from_core_conviction = TRUE
            GROUP BY from_webhook
            ORDER BY from_webhook DESC
        """)
        return cur.fetchall()


def fetch_promotion_candidates(conn):
    """Non-core wallets that meet stable-trader criteria — promotion candidates.

    WR is calculated EXCLUDING timeouts (wins / (wins + losses)). This matches
    the per-wallet display and is the correct way to evaluate signal quality
    when many trades close by 24h timeout near 0%.
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH per_wallet AS (
                SELECT
                    t.triggered_by_wallet AS wallet,
                    w.score, w.is_core,
                    COUNT(*) FILTER (WHERE t.status LIKE 'closed%%') AS closed,
                    SUM(CASE WHEN t.status='closed_tp' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN t.status='closed_sl' THEN 1 ELSE 0 END) AS losses,
                    ROUND(AVG(t.pnl_pct)::numeric, 1) AS mean_pnl,
                    ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.pnl_pct)
                          FILTER (WHERE t.status LIKE 'closed%%')::numeric, 1) AS median_pnl
                FROM wallet_paper_trades t
                JOIN watched_wallets w ON w.address = t.triggered_by_wallet
                WHERE w.is_active = TRUE AND w.is_core = FALSE
                GROUP BY t.triggered_by_wallet, w.score, w.is_core
            )
            SELECT wallet, closed, wins, losses,
                   ROUND(100.0 * wins::numeric / NULLIF(wins+losses, 0), 0) AS wr_decided,
                   mean_pnl, median_pnl, score
            FROM per_wallet
            WHERE closed >= 100
              AND 100.0 * wins::numeric / NULLIF(wins+losses, 0) >= 55
              AND median_pnl >= 0
            ORDER BY wr_decided DESC, median_pnl DESC
            LIMIT 10
        """)
        return cur.fetchall()


def fetch_pool_age_per_wallet(conn):
    """Гипотеза B: распределение pool_age на момент сигнала по core-wallets."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              s.wallet,
              COUNT(*) AS n_with_age,
              ROUND(percentile_cont(0.25) WITHIN GROUP (ORDER BY s.pool_age_at_signal_min)::numeric, 0) AS p25,
              ROUND(percentile_cont(0.50) WITHIN GROUP (ORDER BY s.pool_age_at_signal_min)::numeric, 0) AS p50,
              ROUND(percentile_cont(0.75) WITHIN GROUP (ORDER BY s.pool_age_at_signal_min)::numeric, 0) AS p75,
              SUM(CASE WHEN s.pool_age_at_signal_min < 5 THEN 1 ELSE 0 END) AS lt5min,
              SUM(CASE WHEN s.pool_age_at_signal_min >= 5 AND s.pool_age_at_signal_min < 60 THEN 1 ELSE 0 END) AS lt1h,
              SUM(CASE WHEN s.pool_age_at_signal_min >= 60 THEN 1 ELSE 0 END) AS gte1h
            FROM wallet_signals s
            JOIN watched_wallets w ON w.address = s.wallet AND w.is_core = TRUE
            WHERE s.action = 'buy' AND s.pool_age_at_signal_min IS NOT NULL
            GROUP BY s.wallet
            ORDER BY n_with_age DESC
        """)
        return cur.fetchall()


def fetch_pool_age_pnl_split(conn):
    """Гипотеза B: PnL paper-сделок где сигнал был на свежем (<5min) пуле vs зрелом."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH joined AS (
                SELECT
                    t.id, t.pnl_pct, t.status, s.pool_age_at_signal_min AS age_min
                FROM wallet_paper_trades t
                JOIN wallet_signals s ON s.id = t.signal_id
                WHERE t.from_core_conviction = TRUE
                  AND s.pool_age_at_signal_min IS NOT NULL
            )
            SELECT
              CASE
                WHEN age_min < 5 THEN '< 5 min (fresh pool)'
                WHEN age_min < 60 THEN '5-60 min'
                WHEN age_min < 360 THEN '1-6 hours'
                ELSE '>= 6 hours (mature)'
              END AS bucket,
              COUNT(*) FILTER (WHERE status LIKE 'closed%%') AS closed,
              SUM(CASE WHEN status = 'closed_tp' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN status = 'closed_sl' THEN 1 ELSE 0 END) AS losses,
              ROUND(AVG(pnl_pct) FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS mean,
              ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)
                    FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS median
            FROM joined
            GROUP BY 1
            ORDER BY MIN(COALESCE(age_min, 0))
        """)
        return cur.fetchall()


def fetch_screener_paper_outcomes(conn):
    """Гипотеза C: результаты screener-only paper trades."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              status, COUNT(*),
              ROUND(AVG(pnl_pct)::numeric, 1),
              ROUND(MIN(pnl_pct)::numeric, 1),
              ROUND(MAX(pnl_pct)::numeric, 1),
              ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 1)
            FROM screener_paper_trades
            GROUP BY status ORDER BY COUNT(*) DESC
        """)
        return cur.fetchall()


def fetch_solo_wallet_outcomes(conn):
    """Solo-wallet experimental cohort (введён 2026-05-16)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              solo_wallet_address,
              status,
              COUNT(*),
              ROUND(AVG(pnl_pct)::numeric, 1),
              ROUND(MIN(pnl_pct)::numeric, 1),
              ROUND(MAX(pnl_pct)::numeric, 1),
              ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 1)
            FROM wallet_paper_trades
            WHERE from_solo_wallet = TRUE
            GROUP BY solo_wallet_address, status
            ORDER BY solo_wallet_address, COUNT(*) DESC
        """)
        return cur.fetchall()


def fetch_cohort_comparison(conn):
    """Сравнение wallet-conviction (core-trades) vs screener-only (screener-trades)."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH wallet_cohort AS (
              SELECT 'wallet-conviction' AS cohort,
                     COUNT(*) AS total,
                     COUNT(*) FILTER (WHERE status = 'open') AS open_,
                     COUNT(*) FILTER (WHERE status = 'closed_tp') AS wins,
                     COUNT(*) FILTER (WHERE status = 'closed_sl') AS losses,
                     ROUND(AVG(pnl_pct) FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS mean,
                     ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)
                           FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS median
              FROM wallet_paper_trades WHERE from_core_conviction = TRUE
            ),
            screener_cohort AS (
              SELECT 'screener-only' AS cohort,
                     COUNT(*) AS total,
                     COUNT(*) FILTER (WHERE status = 'open') AS open_,
                     COUNT(*) FILTER (WHERE status = 'closed_tp') AS wins,
                     COUNT(*) FILTER (WHERE status = 'closed_sl') AS losses,
                     ROUND(AVG(pnl_pct) FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS mean,
                     ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)
                           FILTER (WHERE status LIKE 'closed%%')::numeric, 1) AS median
              FROM screener_paper_trades
            )
            SELECT * FROM wallet_cohort
            UNION ALL
            SELECT * FROM screener_cohort
        """)
        return cur.fetchall()


def fetch_core_decline_candidates(conn):
    """Health of core wallets. WR uses decided basis (wins / (wins + losses))
    to be consistent with per-wallet table and the promotion criteria."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH per_wallet AS (
                SELECT
                    t.triggered_by_wallet AS wallet,
                    w.score,
                    COUNT(*) FILTER (WHERE t.status LIKE 'closed%%') AS closed,
                    SUM(CASE WHEN t.status='closed_tp' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN t.status='closed_sl' THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN t.status='closed_timeout' THEN 1 ELSE 0 END) AS timeouts,
                    ROUND(AVG(t.pnl_pct)::numeric, 1) AS mean_pnl,
                    ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.pnl_pct)
                          FILTER (WHERE t.status LIKE 'closed%%')::numeric, 1) AS median_pnl
                FROM wallet_paper_trades t
                JOIN watched_wallets w ON w.address = t.triggered_by_wallet
                WHERE w.is_core = TRUE
                GROUP BY t.triggered_by_wallet, w.score
            )
            SELECT wallet, closed, wins, losses, timeouts,
                   ROUND(100.0 * wins::numeric / NULLIF(wins+losses,0), 0) AS wr_decided,
                   mean_pnl, median_pnl, score
            FROM per_wallet
            ORDER BY median_pnl ASC NULLS LAST
        """)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(database_url: str, md: bool = False) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = []

    H1 = "# " if md else "=== "
    H1_END = "" if md else " ===\n"
    H2 = "## " if md else "\n--- "
    H2_END = "" if md else " ---\n"

    parts.append(f"{H1}Analysis — {now}{H1_END}")

    with db.connect(database_url) as conn:
        cand24, cand24_passed, sig24, prb24, total_trades, open_trades, n_wallets = fetch_activity(conn)
        watcher_rows = fetch_watcher_summary(conn)
        probes_rows = fetch_probes_outcomes(conn)
        cross_tokens = fetch_cross_wallet_tokens(conn)
        conviction_rows = fetch_conviction_comparison(conn)
        wallet_rows = fetch_wallet_cohorts(conn)
        core_strategy_rows = fetch_core_strategy_outcomes(conn)
        latency_rows = fetch_latency_experiment(conn)
        promotion_candidates = fetch_promotion_candidates(conn)
        core_health_rows = fetch_core_decline_candidates(conn)
        # Гипотеза B + C новые секции
        pool_age_per_wallet = fetch_pool_age_per_wallet(conn)
        pool_age_pnl_split = fetch_pool_age_pnl_split(conn)
        screener_paper_rows = fetch_screener_paper_outcomes(conn)
        cohort_comparison_rows = fetch_cohort_comparison(conn)
        solo_rows = fetch_solo_wallet_outcomes(conn)

    # Compute headline numbers
    def _agg(rows, status):
        for r in rows:
            if r[0] == status:
                return r
        return None

    w_open = (_agg(watcher_rows, "open") or (None, 0))[1]
    w_tp = (_agg(watcher_rows, "closed_tp") or (None, 0))[1]
    w_sl = (_agg(watcher_rows, "closed_sl") or (None, 0))[1]
    w_to = (_agg(watcher_rows, "closed_timeout") or (None, 0))[1]
    w_decided = w_tp + w_sl
    w_wr = (w_tp / w_decided * 100) if w_decided else None

    # Avg pnl across closed only
    w_closed_pnls = [(s, n, avg) for s, n, avg, *_ in watcher_rows if s.startswith("closed") and avg is not None]
    w_avg_pnl = None
    if w_closed_pnls:
        total_n = sum(n for _, n, _ in w_closed_pnls)
        w_avg_pnl = sum(float(avg) * n for _, n, avg in w_closed_pnls) / total_n

    # Probes win rate (passed cohort)
    p_passed_row = next((r for r in probes_rows if r[0] is True), None)
    p_rej_row    = next((r for r in probes_rows if r[0] is False), None)
    p_passed_wr = None
    p_rej_wr = None
    if p_passed_row:
        _, _, w, l, _, _, _ = p_passed_row
        if (w + l) > 0:
            p_passed_wr = w / (w + l) * 100
    if p_rej_row:
        _, _, w, l, _, _, _ = p_rej_row
        if (w + l) > 0:
            p_rej_wr = w / (w + l) * 100

    # ==== TL;DR ====
    parts.append(f"\n{H2}TL;DR{H2_END}")
    bullets = []
    if w_decided > 0:
        bullets.append(f"- Watcher (smart-money copy): **{w_wr:.0f}% win rate** "
                       f"({w_tp}W / {w_sl}L / {w_to} timeout / {w_open} open), "
                       f"avg PnL on closed: **{_fmt_pct(w_avg_pnl)}**")
    else:
        bullets.append(f"- Watcher: {w_open} open trades, **0 closed yet** — "
                       f"need ~24h for first TP/SL to land")
    if p_passed_wr is not None:
        bullets.append(f"- Screener-only (probes, passed cohort): **{p_passed_wr:.0f}% win rate**")
    if p_rej_wr is not None:
        bullets.append(f"- Screener rejected baseline: **{p_rej_wr:.0f}% win rate** (random-control)")

    if w_wr is not None and p_passed_wr is not None:
        spread_w_vs_screener = w_wr - p_passed_wr
        bullets.append(f"- **Spread (Watcher − Screener-passed)**: {spread_w_vs_screener:+.0f} pp")
        if abs(spread_w_vs_screener) < 5:
            bullets.append("  - too close to call — either no edge or sample too small")
        elif spread_w_vs_screener >= 10:
            bullets.append("  - **directional evidence Watcher beats Screener**")
        elif spread_w_vs_screener <= -10:
            bullets.append("  - **Watcher underperforms Screener** so far")

    if p_passed_wr is not None and p_rej_wr is not None:
        spread_filter = p_passed_wr - p_rej_wr
        bullets.append(f"- **Filter alpha (passed − rejected)**: {spread_filter:+.0f} pp")
        if abs(spread_filter) < 5:
            bullets.append("  - filters add no measurable edge")

    parts.append("\n".join(bullets) + "\n")

    # ==== Activity ====
    parts.append(f"\n{H2}Activity (last 24h){H2_END}")
    activity_rows = [
        ["distinct tokens detected", cand24],
        ["of which passed filters", cand24_passed],
        ["wallet buy signals", sig24],
        ["price probes recorded", prb24],
    ]
    if md:
        parts.append(_md_table(["metric", "count"], activity_rows))
    else:
        parts.append(_txt_table(["metric", "count"], activity_rows))

    parts.append(f"  ({n_wallets} active wallets, {total_trades} total paper trades, {open_trades} open)\n")

    # ==== Watcher detail ====
    parts.append(f"\n{H2}Watcher paper-trade outcomes{H2_END}")
    if watcher_rows:
        rows = [[s, n, _fmt_pct(avg), _fmt_pct(w), _fmt_pct(b)]
                for s, n, avg, w, b in watcher_rows]
        cols = ["status", "n", "avg pnl", "worst", "best"]
    else:
        rows, cols = [], ["status", "n", "avg pnl", "worst", "best"]
    parts.append((_md_table if md else _txt_table)(cols, rows))

    # ==== Probes detail ====
    parts.append(f"\n{H2}Screener probes (simulated +18/-12 from first detect){H2_END}")
    if probes_rows:
        rows = []
        for passed, tokens, w, l, o, peak, trough in probes_rows:
            label = "passed filters" if passed else ("rejected" if passed is False else "unclassified")
            decided = w + l
            wr = f"{(w/decided*100):.0f}%" if decided else "n/a"
            ev = ((w/decided)*18 + (l/decided)*-12) if decided else None
            rows.append([label, tokens, w, l, o, wr, _fmt_pct(ev),
                         _fmt_pct(peak), _fmt_pct(trough)])
        cols = ["cohort", "tokens", "wins", "losses", "open",
                "win_rate", "EV", "avg_peak", "avg_trough"]
    else:
        rows, cols = [], []
    parts.append((_md_table if md else _txt_table)(cols, rows))

    # ==== Cross-wallet conviction ====
    parts.append(f"\n{H2}Cross-wallet conviction (≥2 wallets, last 48h){H2_END}")
    if cross_tokens:
        rows = []
        for addr, sym, nw, ns, op, w, l, avg in cross_tokens:
            rows.append([sym or addr[:8], nw, ns, op, w, l, _fmt_pct(avg)])
        cols = ["symbol", "wallets", "signals", "open", "wins", "losses", "avg_pnl"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (no cross-wallet signals yet)\n")

    # ==== Conviction comparison ====
    parts.append(f"\n{H2}Conviction cohort: cross-wallet vs single-wallet{H2_END}")
    if conviction_rows:
        rows = []
        for cohort, n, w, l, closed, avg in conviction_rows:
            decided = w + l
            wr = f"{(w/decided*100):.0f}%" if decided else "n/a"
            rows.append([cohort, n, w, l, closed, wr, _fmt_pct(avg)])
        cols = ["cohort", "trades", "wins", "losses", "closed", "win_rate", "avg_pnl"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (no data yet)\n")

    # ==== Per-wallet ====
    parts.append(f"\n{H2}Per-wallet trade outcomes{H2_END}")
    if wallet_rows:
        rows = []
        for w_addr, score, is_core, age_h, trades, wins, losses, closed, avg, median in wallet_rows:
            decided = wins + losses
            wr = f"{(wins/decided*100):.0f}%" if decided else "n/a"
            tag = "★" if is_core else " "
            rows.append([tag + " " + w_addr[:12] + "…", float(score or 0), float(age_h),
                         trades, wins, losses, closed, wr,
                         _fmt_pct(avg), _fmt_pct(median)])
        cols = ["wallet (★=core)", "score", "age_h", "trades", "wins", "losses",
                "closed", "win_rate", "avg_pnl", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (no per-wallet data yet)\n")

    # ==== Core strategy (Уровень 2) ====
    parts.append(f"\n{H2}Core strategy outcomes (cross-wallet conviction trades only){H2_END}")
    if core_strategy_rows:
        rows = [[s, n, _fmt_pct(avg), _fmt_pct(w), _fmt_pct(b), _fmt_pct(med)]
                for s, n, avg, w, b, med in core_strategy_rows]
        cols = ["status", "n", "avg pnl", "worst", "best", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (no core-conviction trades opened yet — нужно ≥2 core-кошелька на одном токене)\n")

    # ==== Latency experiment ====
    parts.append(f"\n{H2}Latency experiment: webhook vs cron{H2_END}")
    parts.append("  Сравнение когорт open paper trades. Одинаковая стратегия (cross-wallet ≥2 core),\n"
                 "  одинаковые TP+18% / SL-12% / timeout 24h. Различие — только задержка детекции:\n"
                 "  webhook ~5-15 сек, cron ~10 мин. Спред mean PnL = эффект устранения задержки.\n")
    if latency_rows:
        rows = []
        for src, n, op, w, l, t, mean, median in latency_rows:
            decided = (w or 0) + (l or 0)
            wr = f"{int(100*w/decided)}%" if decided else "n/a"
            rows.append([src, n, op, w, l, t, wr, _fmt_pct(mean), _fmt_pct(median)])
        cols = ["source", "total", "open", "W", "L", "T", "wr_dec", "mean", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (нет core-сделок ни в одной когорте)\n")

    # ==== Core health ====
    parts.append(f"\n{H2}Health of current core wallets{H2_END}")
    parts.append("  WR на decided basis (без таймаутов). timeout% — доля сделок не дошедших до TP/SL за 24ч.\n")
    if core_health_rows:
        rows = []
        for w_addr, closed, wins, losses, timeouts, wr, mean_pnl, median_pnl, score in core_health_rows:
            wr_s = f"{int(wr)}%" if wr is not None else "n/a"
            timeout_pct = f"{int(100.0 * timeouts / closed)}%" if closed else "n/a"
            rows.append([w_addr[:14] + "…", closed, wins, losses, timeouts,
                         wr_s, timeout_pct,
                         _fmt_pct(mean_pnl), _fmt_pct(median_pnl), float(score or 0)])
        cols = ["wallet", "closed", "W", "L", "T",
                "wr_decided", "timeout%", "mean", "median", "score"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (no core wallets defined)\n")

    # ==== Promotion candidates ====
    parts.append(f"\n{H2}Promotion candidates (non-core wallets matching stable criteria){H2_END}")
    parts.append("  Критерий: ≥100 закрытых, WR ≥55% (без таймаутов), медиана PnL ≥0%\n")
    if promotion_candidates:
        rows = []
        for w_addr, closed, wins, losses, wr, mean_pnl, median_pnl, score in promotion_candidates:
            wr_s = f"{int(wr)}%" if wr is not None else "n/a"
            rows.append([w_addr[:14] + "…", closed, wins, losses, wr_s,
                         _fmt_pct(mean_pnl), _fmt_pct(median_pnl), float(score or 0)])
        cols = ["wallet", "closed", "wins", "losses", "wr_decided", "mean_pnl", "median", "score"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
        parts.append("  Чтобы добавить в core: `python -m dexbot.discovery promote ADDR`\n")
    else:
        parts.append("  (пока нет достойных кандидатов — нужно ≥100 закрытых сделок на кошельке)\n")

    # ==== Гипотеза B: Pool age per core wallet ====
    parts.append(f"\n{H2}Pool age per core wallet (Гипотеза B){H2_END}")
    parts.append("  Возраст пула в МИНУТАХ на момент покупки core-кошельком.\n"
                 "  Свежие пулы (<5 мин) = снайперский вход, мы их не догоняем по latency.\n"
                 "  Зрелые пулы (>60 мин) = воспроизводимы — успели проиндексироваться, цена стабильна.\n")
    if pool_age_per_wallet:
        rows = []
        for wallet, n, p25, p50, p75, lt5, lt1h, gte1h in pool_age_per_wallet:
            rows.append([wallet[:14] + "…", n, int(p25 or 0), int(p50 or 0), int(p75 or 0),
                         lt5, lt1h, gte1h])
        cols = ["wallet", "n", "p25_min", "median_min", "p75_min", "<5min", "5-60min", "≥1h"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (нет данных — запусти `python -m dexbot.backfill_pool_age --core-only`)\n")

    parts.append(f"\n{H2}Pool age × PnL split (Гипотеза B){H2_END}")
    parts.append("  Core-сделки разбитые по возрасту пула на момент сигнала.\n"
                 "  Если sweet-spot выявит себя — это станет фильтром.\n")
    if pool_age_pnl_split:
        rows = []
        for bucket, closed, wins, losses, mean, median in pool_age_pnl_split:
            decided = (wins or 0) + (losses or 0)
            wr = f"{int(100*wins/decided)}%" if decided else "n/a"
            rows.append([bucket, closed or 0, wins or 0, losses or 0, wr,
                         _fmt_pct(mean), _fmt_pct(median)])
        cols = ["pool age bucket", "closed", "W", "L", "WR", "mean_pnl", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (нет данных — paper-сделки ещё без связанной pool_age колонки)\n")

    # ==== Гипотеза C: Screener-only paper trades ====
    parts.append(f"\n{H2}Screener-only paper trades (Гипотеза C){H2_END}")
    parts.append("  Независимая когорта: открываем сделку на КАЖДЫЙ passed-кандидат "
                 "(не связано с wallet-conviction). TP +18% / SL −12% / max_hold 168h.\n")
    if screener_paper_rows:
        rows = [[s, n, _fmt_pct(avg), _fmt_pct(worst), _fmt_pct(best), _fmt_pct(med)]
                for s, n, avg, worst, best, med in screener_paper_rows]
        cols = ["status", "n", "mean_pnl", "worst", "best", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (нет сделок — screener_trader подключится при следующем screener-cron tick)\n")

    # ==== Solo-wallet experiment ====
    parts.append(f"\n{H2}Solo-wallet experimental cohort (с 2026-05-16){H2_END}")
    parts.append("  Открываем paper-сделку на КАЖДЫЙ buy конкретных кошельков, без cross-wallet, мгновенно.\n"
                 "  Цель — проверить переносится ли их легаси WR в новую систему.\n")
    if solo_rows:
        rows = []
        for addr, st, n, mean, worst, best, med in solo_rows:
            rows.append([addr[:14] + "…", st, n,
                         _fmt_pct(mean), _fmt_pct(worst), _fmt_pct(best), _fmt_pct(med)])
        cols = ["wallet", "status", "n", "mean", "worst", "best", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))
    else:
        parts.append("  (пока нет solo-сделок — ждём первой покупки от Gvy после деплоя)\n")

    parts.append(f"\n{H2}Cohort comparison: wallet-conviction vs screener-only{H2_END}")
    if cohort_comparison_rows:
        rows = []
        for cohort, total, op, wins, losses, mean, median in cohort_comparison_rows:
            decided = (wins or 0) + (losses or 0)
            wr = f"{int(100*wins/decided)}%" if decided else "n/a"
            rows.append([cohort, total or 0, op or 0, wins or 0, losses or 0, wr,
                         _fmt_pct(mean), _fmt_pct(median)])
        cols = ["cohort", "total", "open", "W", "L", "WR", "mean_pnl", "median"]
        parts.append((_md_table if md else _txt_table)(cols, rows))

    # ==== Verdict ====
    parts.append(f"\n{H2}Read this as{H2_END}")
    verdicts = []
    if w_decided < 10:
        verdicts.append("- **Sample too small for conclusions.** Need ≥30 closed trades before trusting numbers.")
    elif w_decided < 30:
        verdicts.append(f"- **{w_decided} closed trades** — early signal but still wide error bars. Wait for ≥50.")
    else:
        verdicts.append(f"- **{w_decided} closed trades** — sample becoming meaningful.")

    if w_avg_pnl is not None:
        if w_avg_pnl < -1:
            verdicts.append(f"  - Watcher is **net negative** ({_fmt_pct(w_avg_pnl)}). Either signal is noise, or +18/-12 is wrong target.")
        elif w_avg_pnl < 2:
            verdicts.append(f"  - Watcher is **near break-even** ({_fmt_pct(w_avg_pnl)}). Might be edge, might be noise — need more data.")
        else:
            verdicts.append(f"  - Watcher shows **positive expectancy** ({_fmt_pct(w_avg_pnl)}). Promising — keep collecting.")

    # Per-wallet skew
    if wallet_rows:
        strong = [w for w in wallet_rows if (w[6] or 0) >= 5 and (w[7] or 0) >= 60]
        weak = [w for w in wallet_rows if (w[6] or 0) >= 5 and (w[4] or 0) == 0]
        if strong:
            verdicts.append(f"  - {len(strong)} wallet(s) with ≥5 closed trades and ≥60% win rate — focus area.")
        if weak:
            verdicts.append(f"  - {len(weak)} wallet(s) with ≥5 closed trades and **0 wins** — candidates to deactivate.")
    parts.append("\n".join(verdicts) + "\n")

    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dexbot.analysis")
    p.add_argument("--markdown", action="store_true",
                   help="Emit GH-flavored markdown (for $GITHUB_STEP_SUMMARY).")
    args = p.parse_args(argv)

    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    print(render(config.database_url, md=args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
