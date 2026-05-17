#!/usr/bin/env python3
"""
Paper-test сканер cross-DEX арбитража (Raydium / Orca / Meteora).
Источник цен: DexScreener API (уже используется в проекте).
Без реальных сделок — пишет ВСЕ замеры спреда (даже отрицательные после
комиссий) в arb_opportunities.csv. Так получаем полное распределение,
а не только редкие "положительные" пики.

Запуск:
    python -m dexbot.arb_scanner               # бесконечный цикл (Ctrl+C)
    python -m dexbot.arb_scanner --once        # один прогон всех токенов
    python -m dexbot.arb_scanner --interval 30 # интервал, сек (default: 60)
    python -m dexbot.arb_scanner --summary     # статистика по CSV
"""
from __future__ import annotations

import argparse
import csv
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com"

# DEX → комиссия за один своп (%)
DEX_FEE_PCT: dict[str, float] = {
    "raydium":  0.25,
    "orca":     0.30,
    "meteora":  0.20,
    "meteora-dlmm": 0.20,
}
TARGET_DEXES = set(DEX_FEE_PCT.keys())

# Симулируем $20 на вход
AMOUNT_USD = 20.0

# Минимальная ликвидность пула — игнорируем мелкие пулы (высокий slippage)
MIN_LIQUIDITY_USD = 50_000

# Максимальный GROSS спред — выше почти всегда означает разные quote-токены или stale data
MAX_GROSS_SPREAD_PCT = 8.0

# Порог для лог-сообщения "ARB ..." в stdout (CSV пишет ВСЕ замеры)
LOG_NET_SPREAD_PCT = 0.05

OUTPUT_CSV = Path("arb_opportunities.csv")

CSV_FIELDS = [
    "ts", "symbol", "mint",
    "buy_dex", "sell_dex",
    "buy_price_usd", "sell_price_usd",
    "buy_liquidity_usd", "sell_liquidity_usd",
    "gross_spread_pct", "total_fee_pct", "net_spread_pct",
    "profit_usd_on_20",
]

# Список токенов (symbol, mint) — смесь major и mid-cap для разных tier'ов MEV-конкуренции
WATCH_TOKENS: list[tuple[str, str]] = [
    # Major (топ MEV-конкуренция, ожидаем спреды ~0)
    ("SOL",       "So11111111111111111111111111111111111111112"),
    ("BONK",      "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"),
    ("WIF",       "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    ("POPCAT",    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"),
    ("TRUMP",     "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"),
    # Mid-cap (часть ботов их игнорирует)
    ("FARTCOIN",  "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"),
    ("MEW",       "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5"),
    ("SLERF",     "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7ByyfFg5"),
    ("PNUT",      "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"),
    ("GOAT",      "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump"),
    ("MOODENG",   "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY"),
    ("ai16z",     "HeLp6NuQkmYB4pYWo2zYs22mESHXPQYzXbB8n4V98jwC"),
    ("ACT",       "GJAFwWjJ3vnTsrQVabjBVK2TYB1YtRCQXRDfDgUnpump"),
    ("PENGU",     "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv"),
    ("CHILLGUY",  "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump"),
]


# ── DexScreener helpers ───────────────────────────────────────────────────────

def _fetch_pairs(mint: str) -> list[dict]:
    url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception as e:
        log.warning("DexScreener error for %s: %s", mint, e)
        return []


def best_price_per_dex(mint: str) -> dict[str, dict]:
    """
    Для каждого DEX возвращает пул с наибольшей ликвидностью:
    {dex_id: {price_usd, liquidity_usd, pair_address}}
    """
    pairs = _fetch_pairs(mint)
    # Фильтр: только Solana, только нужные DEX, только с ценой
    solana_pairs = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and p.get("dexId") in TARGET_DEXES
        and p.get("priceUsd")
        and (p.get("liquidity") or {}).get("usd", 0) >= MIN_LIQUIDITY_USD
    ]

    best: dict[str, dict] = {}
    for p in solana_pairs:
        dex = p["dexId"]
        liq = (p.get("liquidity") or {}).get("usd", 0)
        existing = best.get(dex)
        if existing is None or liq > existing["liquidity_usd"]:
            best[dex] = {
                "price_usd": float(p["priceUsd"]),
                "liquidity_usd": liq,
                "pair_address": p.get("pairAddress", ""),
            }
    return best


# ── Анализ спреда ────────────────────────────────────────────────────────────

def analyze_token(symbol: str, mint: str) -> list[dict]:
    prices = best_price_per_dex(mint)
    if len(prices) < 2:
        log.debug("%s: только %d DEX с ликвидностью — пропускаем", symbol, len(prices))
        return []

    for dex, info in prices.items():
        log.debug("  %s @ %-14s $%-12.6f  liq=$%.0f",
                  symbol, dex, info["price_usd"], info["liquidity_usd"])

    ts = datetime.now(timezone.utc).isoformat()
    opportunities = []
    dex_list = list(prices.keys())

    for i in range(len(dex_list)):
        for j in range(len(dex_list)):
            if i == j:
                continue
            buy_dex = dex_list[i]
            sell_dex = dex_list[j]
            buy_price = prices[buy_dex]["price_usd"]
            sell_price = prices[sell_dex]["price_usd"]

            if sell_price <= buy_price:
                continue

            gross_pct = (sell_price / buy_price - 1) * 100

            # Фильтр: >8% — почти точно разные quote-токены или stale data
            if gross_pct > MAX_GROSS_SPREAD_PCT:
                log.debug("  SKIP %s %s→%s gross=%.1f%% (вероятно разные пары)",
                          symbol, buy_dex, sell_dex, gross_pct)
                continue

            # Нормализуем имя DEX для поиска в таблице комиссий
            def _fee(d: str) -> float:
                return DEX_FEE_PCT.get(d, 0.30)

            total_fee_pct = _fee(buy_dex) + _fee(sell_dex)
            net_pct = gross_pct - total_fee_pct
            profit_usd = AMOUNT_USD * (net_pct / 100)

            opp = {
                "ts": ts,
                "symbol": symbol,
                "mint": mint,
                "buy_dex": buy_dex,
                "sell_dex": sell_dex,
                "buy_price_usd": round(buy_price, 8),
                "sell_price_usd": round(sell_price, 8),
                "buy_liquidity_usd": round(prices[buy_dex]["liquidity_usd"]),
                "sell_liquidity_usd": round(prices[sell_dex]["liquidity_usd"]),
                "gross_spread_pct": round(gross_pct, 4),
                "total_fee_pct": round(total_fee_pct, 4),
                "net_spread_pct": round(net_pct, 4),
                "profit_usd_on_20": round(profit_usd, 4),
            }
            opportunities.append(opp)  # пишем ВСЕ замеры — для распределения

            # В лог печатаем только "значимые" — иначе stdout захлёбывается
            if net_pct >= LOG_NET_SPREAD_PCT:
                log.info(
                    "ARB %-8s  buy@%-12s sell@%-12s  gross=%.3f%%  fee=%.2f%%  NET=%.3f%%  $%.4f",
                    symbol, buy_dex, sell_dex, gross_pct, total_fee_pct, net_pct, profit_usd,
                )

    return opportunities


# ── CSV ───────────────────────────────────────────────────────────────────────

def _ensure_csv_header() -> None:
    if not OUTPUT_CSV.exists():
        with OUTPUT_CSV.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def _write_opportunities(opps: list[dict]) -> None:
    if not opps:
        return
    with OUTPUT_CSV.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerows(opps)


# ── Циклы ─────────────────────────────────────────────────────────────────────

def run_once() -> int:
    _ensure_csv_header()
    total = 0
    for symbol, mint in WATCH_TOKENS:
        log.info("Сканирую %s ...", symbol)
        opps = analyze_token(symbol, mint)
        _write_opportunities(opps)
        total += len(opps)
        time.sleep(0.5)  # не спамим DexScreener
    return total


def run_loop(interval_sec: int = 60) -> None:
    _ensure_csv_header()
    log.info("Арбитражный сканер запущен. Интервал: %ds. Ctrl+C для выхода.", interval_sec)
    log.info("Результаты → %s", OUTPUT_CSV.resolve())
    cycle = 0
    while True:
        cycle += 1
        log.info("=== Цикл #%d ===", cycle)
        found = run_once()
        log.info("Цикл #%d завершён: %d замеров записано", cycle, found)
        time.sleep(interval_sec)


# ── Summary ───────────────────────────────────────────────────────────────────

def _pct(values: list[float], p: float) -> float:
    """Перцентиль без numpy. p в долях (0.5 = медиана)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _share(values: list[float], threshold: float) -> float:
    """Доля замеров >= threshold, в процентах."""
    if not values:
        return 0.0
    return sum(1 for v in values if v >= threshold) / len(values) * 100


def print_summary() -> None:
    if not OUTPUT_CSV.exists():
        print("CSV не найден. Запустите сканер сначала.")
        return

    with OUTPUT_CSV.open() as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("CSV пустой.")
        return

    print(f"\n=== Всего замеров: {len(rows)} ===")
    print(f"Период: {rows[0]['ts']} → {rows[-1]['ts']}\n")

    # Группируем
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_pair:   dict[str, list[float]] = defaultdict(list)
    all_nets:  list[float] = []

    for r in rows:
        net = float(r["net_spread_pct"])
        by_symbol[r["symbol"]].append(net)
        by_pair[f"{r['buy_dex']}→{r['sell_dex']}"].append(net)
        all_nets.append(net)

    # ── Распределение NET по всем замерам ────────────────────────────────────
    print("── Распределение NET спреда (после комиссий, %) ──")
    print(f"  p25 = {_pct(all_nets, 0.25):>7.3f}")
    print(f"  p50 = {_pct(all_nets, 0.50):>7.3f}   ← медиана")
    print(f"  p75 = {_pct(all_nets, 0.75):>7.3f}")
    print(f"  p90 = {_pct(all_nets, 0.90):>7.3f}")
    print(f"  p99 = {_pct(all_nets, 0.99):>7.3f}")
    print(f"  max = {max(all_nets):>7.3f}")

    print("\n── Доля замеров выше порога ──")
    print(f"  NET > 0.0%   = {_share(all_nets, 0.0):5.1f}%  (теоретически профитно)")
    print(f"  NET > 0.2%   = {_share(all_nets, 0.2):5.1f}%  (покрывает gas+slippage)")
    print(f"  NET > 0.5%   = {_share(all_nets, 0.5):5.1f}%  (покрывает Jito tip)")
    print(f"  NET > 1.0%   = {_share(all_nets, 1.0):5.1f}%  (комфортная маржа)")

    # ── По токену ────────────────────────────────────────────────────────────
    print(f"\n── По токену (отсортировано по медиане NET) ──")
    print(f"{'Symbol':<10} {'N':>5} {'p50':>8} {'p90':>8} {'p99':>8} {'>0.5%':>8}")
    symbol_rows = [
        (s, len(v), _pct(v, 0.5), _pct(v, 0.9), _pct(v, 0.99), _share(v, 0.5))
        for s, v in by_symbol.items()
    ]
    for s, n, p50, p90, p99, sh in sorted(symbol_rows, key=lambda x: -x[2]):
        print(f"{s:<10} {n:>5} {p50:>8.3f} {p90:>8.3f} {p99:>8.3f} {sh:>7.1f}%")

    # ── По паре DEX ──────────────────────────────────────────────────────────
    print(f"\n── По направлению свапа ──")
    print(f"{'buy → sell':<28} {'N':>5} {'p50':>8} {'p90':>8} {'>0.5%':>8}")
    pair_rows = [
        (p, len(v), _pct(v, 0.5), _pct(v, 0.9), _share(v, 0.5))
        for p, v in by_pair.items()
    ]
    for p, n, p50, p90, sh in sorted(pair_rows, key=lambda x: -x[2]):
        print(f"{p:<28} {n:>5} {p50:>8.3f} {p90:>8.3f} {sh:>7.1f}%")

    # ── Paper P&L при разных стратегиях исполнения ───────────────────────────
    print(f"\n── Paper P&L при $20/сделка ──")
    only_profitable = [n for n in all_nets if n > 0]
    only_realistic  = [n for n in all_nets if n > 0.5]
    print(f"  Если ловим ВСЕ NET > 0:    {len(only_profitable):>4} сделок, "
          f"profit = ${sum(only_profitable) * AMOUNT_USD / 100:>7.2f}")
    print(f"  Если ловим только NET>0.5: {len(only_realistic):>4} сделок, "
          f"profit = ${sum(only_realistic) * AMOUNT_USD / 100:>7.2f}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cross-DEX арбитраж paper-тест")
    parser.add_argument("--once",     action="store_true", help="Один прогон и выход")
    parser.add_argument("--interval", type=int, default=60, help="Интервал между циклами, сек")
    parser.add_argument("--summary",  action="store_true", help="Статистика по CSV")
    parser.add_argument("--debug",    action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.summary:
        print_summary()
    elif args.once:
        n = run_once()
        log.info("Готово. %d замеров → %s", n, OUTPUT_CSV.resolve())
    else:
        run_loop(args.interval)
