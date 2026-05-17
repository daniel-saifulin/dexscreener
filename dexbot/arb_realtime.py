#!/usr/bin/env python3
"""
Realtime cross-DEX арбитражный сканер для Solana мемкоинов.

Что делает:
  - Discovery: находит пулы каждого токена на Orca Whirlpool и Raydium AMM v4
    через DexScreener, читает on-chain metadata (mint addresses, decimals, vaults).
  - Tick loop: каждую секунду делает один batched getMultipleAccounts через
    Helius RPC, декодирует цены on-chain, сравнивает между DEX'ами.
  - Логирует net-positive opportunities в arb_realtime_memes.csv.

Что не делает:
  - Не поддерживает Meteora DLMM (bin-математика сложная).
  - Не поддерживает Raydium CLMM (концентрированная ликвидность v3).
  - Не учитывает price impact и Jito tips — это "теоретический edge".

Запуск:
    python -m dexbot.arb_realtime                  # 5 минут
    python -m dexbot.arb_realtime --duration 1800  # 30 минут
    python -m dexbot.arb_realtime --interval 0.5   # 2 Hz
"""
from __future__ import annotations

import argparse
import base64
import csv
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# ── Минты и quote-токены ──────────────────────────────────────────────────────
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {USDC_MINT, WSOL_MINT}

# Cache: mint → decimals
KNOWN_DECIMALS: dict[str, int] = {USDC_MINT: 6, WSOL_MINT: 9}

# ── DEX комиссии (round-trip считается отдельно) ──────────────────────────────
DEFAULT_FEES = {"orca-whirlpool": 0.30, "raydium-amm": 0.25}

# ── Layout offsets ────────────────────────────────────────────────────────────
# Orca Whirlpool (653 байта)
WP_SQRT_PRICE = 65
WP_TOKEN_MINT_A = 101
WP_TOKEN_MINT_B = 181
WP_DATA_LEN = 653

# Raydium AMM v4 (752 байта)
RAY_BASE_DEC = 32
RAY_QUOTE_DEC = 40
RAY_BASE_VAULT = 336
RAY_QUOTE_VAULT = 368
RAY_BASE_MINT = 400
RAY_QUOTE_MINT = 432
RAY_DATA_LEN = 752

# SPL Token account
SPL_AMOUNT_OFFSET = 64

# SPL Mint
MINT_DECIMALS_OFFSET = 44

# ── Параметры ─────────────────────────────────────────────────────────────────
MIN_LIQUIDITY_USD = 30_000
MIN_NET_SPREAD_PCT = 0.05
MAX_GROSS_SPREAD_PCT = 8.0  # фильтр от разных quote-токенов / stale data

OUTPUT_CSV = Path("arb_realtime_memes.csv")
CSV_FIELDS = [
    "ts", "symbol", "quote", "buy_dex", "sell_dex",
    "buy_price", "sell_price",
    "gross_pct", "fee_pct", "net_pct",
    "profit_on_20_usd",
    "buy_pool", "sell_pool",
]

# ── Список токенов для мониторинга (топ-20 Solana мемов + WSOL контроль) ──────
# Адреса лучше brand-известны; discovery пропустит токены без ликвидных пулов.
DEFAULT_TOKENS: list[tuple[str, str]] = [
    # Текущий watch list (7)
    ("BONK",     "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"),
    ("WIF",      "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    ("POPCAT",   "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"),
    ("TRUMP",    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"),
    ("FARTCOIN", "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"),
    ("MEW",      "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5"),
    ("SLERF",    "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7ByyfFg5"),
    # Расширение до 20 (+13)
    ("BOME",     "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82"),
    ("PNUT",     "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"),
    ("MOODENG",  "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY"),
    ("GOAT",     "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump"),
    ("MOTHER",   "3S8qX1MsMqRbiwKg2cQyx7nis1oHMgaCuc9c4VfvVdPN"),
    ("GIGA",     "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9"),
    ("MICHI",    "5mbK36SZ7J19An8jFochhQS4of8g6BwUjbeCSxBSoWdp"),
    ("CHILLGUY", "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump"),
    ("PONKE",    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC"),
    ("ACT",      "GJAFwWjJ3vnTsrQVabjBVK2TYB1YtRCQXRDfDgUnpump"),
    ("RETARDIO", "6ogzHhzdrQr9Pgv6hZ2MNze7UrzBMAFyBBWUYp1Fhitx"),
    ("SPX",      "J3NKxxXZcnNiMjKw9hYb2K4LUxgwB6t1FtPtQVsv3KFr"),
    ("NEIRO",    "CTg3ZgYx55nnBHaPB9CmKn8nM7uXq7E1uMa6cWxdpump"),
    # Контрольная группа — должна показать 0 opportunities
    ("WSOL",     "So11111111111111111111111111111111111111112"),
]


# ── Base58 (vendored, без зависимостей) ───────────────────────────────────────
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n_zeros = 0
    for b in data:
        if b == 0:
            n_zeros += 1
        else:
            break
    num = int.from_bytes(data, "big")
    result = []
    while num > 0:
        num, mod = divmod(num, 58)
        result.append(_B58_ALPHABET[mod])
    return "1" * n_zeros + "".join(reversed(result))


# ── Pool ──────────────────────────────────────────────────────────────────────

@dataclass
class Pool:
    symbol: str
    target_mint: str
    quote_mint: str
    target_decimals: int
    quote_decimals: int
    dex: str
    pool_address: str
    liquidity_usd: float
    fee_pct: float
    # Orca-only
    target_is_a: bool = False
    # Raydium-only
    base_vault: str = ""
    quote_vault: str = ""
    target_is_base: bool = True


# ── RPC ───────────────────────────────────────────────────────────────────────

def _rpc_url() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY не задан в .env")
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


def get_account_info(rpc_url: str, address: str) -> bytes | None:
    try:
        r = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [address, {"encoding": "base64", "commitment": "confirmed"}],
        }, timeout=10)
        r.raise_for_status()
        result = r.json().get("result")
        if not result or not result.get("value"):
            return None
        return base64.b64decode(result["value"]["data"][0])
    except Exception as e:
        log.warning("RPC error %s: %s", address[:8], e)
        return None


def get_multiple_accounts(rpc_url: str, addresses: list[str]) -> list[bytes | None]:
    if not addresses:
        return []
    out: list[bytes | None] = []
    # Solana RPC: до 100 аккаунтов за запрос
    for i in range(0, len(addresses), 100):
        chunk = addresses[i:i + 100]
        try:
            r = requests.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getMultipleAccounts",
                "params": [chunk, {"encoding": "base64", "commitment": "processed"}],
            }, timeout=10)
            r.raise_for_status()
            values = r.json().get("result", {}).get("value", [])
            for v in values:
                if v is None or not v.get("data"):
                    out.append(None)
                else:
                    out.append(base64.b64decode(v["data"][0]))
        except Exception as e:
            log.warning("Batch RPC error: %s", e)
            out.extend([None] * len(chunk))
    return out


def fetch_mint_decimals(rpc_url: str, mint: str) -> int | None:
    if mint in KNOWN_DECIMALS:
        return KNOWN_DECIMALS[mint]
    data = get_account_info(rpc_url, mint)
    if data is None or len(data) <= MINT_DECIMALS_OFFSET:
        return None
    decimals = data[MINT_DECIMALS_OFFSET]
    KNOWN_DECIMALS[mint] = decimals
    return decimals


# ── DexScreener ───────────────────────────────────────────────────────────────

def fetch_pairs(mint: str) -> list[dict]:
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=10)
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception as e:
        log.warning("DexScreener error %s: %s", mint[:8], e)
        return []


# ── Discovery: построение Pool из пары DexScreener ────────────────────────────

def _build_orca_pool(symbol: str, target_mint: str, pair: dict, rpc: str) -> Pool | None:
    pool_addr = pair["pairAddress"]
    data = get_account_info(rpc, pool_addr)
    if data is None or len(data) != WP_DATA_LEN:
        log.debug("  %s Orca %s: skip (data_len=%s, expected=%d)",
                  symbol, pool_addr[:8], len(data) if data else "None", WP_DATA_LEN)
        return None

    mint_a = b58encode(data[WP_TOKEN_MINT_A:WP_TOKEN_MINT_A + 32])
    mint_b = b58encode(data[WP_TOKEN_MINT_B:WP_TOKEN_MINT_B + 32])

    if target_mint == mint_a:
        target_is_a, quote_mint = True, mint_b
    elif target_mint == mint_b:
        target_is_a, quote_mint = False, mint_a
    else:
        return None

    if quote_mint not in QUOTE_MINTS:
        return None

    target_dec = fetch_mint_decimals(rpc, target_mint)
    quote_dec = fetch_mint_decimals(rpc, quote_mint)
    if target_dec is None or quote_dec is None:
        return None

    return Pool(
        symbol=symbol, target_mint=target_mint, quote_mint=quote_mint,
        target_decimals=target_dec, quote_decimals=quote_dec,
        dex="orca-whirlpool", pool_address=pool_addr,
        liquidity_usd=(pair.get("liquidity") or {}).get("usd", 0),
        fee_pct=DEFAULT_FEES["orca-whirlpool"],
        target_is_a=target_is_a,
    )


def _build_raydium_pool(symbol: str, target_mint: str, pair: dict, rpc: str) -> Pool | None:
    pool_addr = pair["pairAddress"]
    data = get_account_info(rpc, pool_addr)
    if data is None or len(data) != RAY_DATA_LEN:
        log.debug("  %s Raydium %s: skip (data_len=%s, expected=%d)",
                  symbol, pool_addr[:8], len(data) if data else "None", RAY_DATA_LEN)
        return None

    base_mint = b58encode(data[RAY_BASE_MINT:RAY_BASE_MINT + 32])
    quote_mint = b58encode(data[RAY_QUOTE_MINT:RAY_QUOTE_MINT + 32])
    base_vault = b58encode(data[RAY_BASE_VAULT:RAY_BASE_VAULT + 32])
    quote_vault = b58encode(data[RAY_QUOTE_VAULT:RAY_QUOTE_VAULT + 32])

    if target_mint == base_mint:
        target_is_base, usd_mint = True, quote_mint
    elif target_mint == quote_mint:
        target_is_base, usd_mint = False, base_mint
    else:
        return None

    if usd_mint not in QUOTE_MINTS:
        return None

    base_dec = int.from_bytes(data[RAY_BASE_DEC:RAY_BASE_DEC + 8], "little")
    quote_dec_field = int.from_bytes(data[RAY_QUOTE_DEC:RAY_QUOTE_DEC + 8], "little")
    if target_is_base:
        target_dec, usd_dec = base_dec, quote_dec_field
    else:
        target_dec, usd_dec = quote_dec_field, base_dec

    # Sanity check на decimals (1-18 — нормальный диапазон)
    if not (0 < target_dec <= 18) or not (0 < usd_dec <= 18):
        return None

    return Pool(
        symbol=symbol, target_mint=target_mint, quote_mint=usd_mint,
        target_decimals=target_dec, quote_decimals=usd_dec,
        dex="raydium-amm", pool_address=pool_addr,
        liquidity_usd=(pair.get("liquidity") or {}).get("usd", 0),
        fee_pct=DEFAULT_FEES["raydium-amm"],
        base_vault=base_vault, quote_vault=quote_vault,
        target_is_base=target_is_base,
    )


def discover_pools(tokens: list[tuple[str, str]], rpc: str) -> list[Pool]:
    pools: list[Pool] = []
    for symbol, mint in tokens:
        log.info("Discovery: %s ...", symbol)
        pairs = fetch_pairs(mint)
        liquid = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and (p.get("liquidity") or {}).get("usd", 0) >= MIN_LIQUIDITY_USD
        ]
        # Самые ликвидные первые
        liquid.sort(key=lambda p: -(p.get("liquidity") or {}).get("usd", 0))

        for dex_id, builder in [("orca", _build_orca_pool), ("raydium", _build_raydium_pool)]:
            for cand in [p for p in liquid if p.get("dexId") == dex_id][:5]:
                pool = builder(symbol, mint, cand, rpc)
                if pool:
                    pools.append(pool)
                    log.info("  + %-14s liq=$%-12s quote=%s pool=%s",
                             pool.dex, f"{pool.liquidity_usd:,.0f}",
                             "USDC" if pool.quote_mint == USDC_MINT else "SOL",
                             pool.pool_address[:8])
                    break  # один пул на DEX
        time.sleep(0.3)  # вежливо к DexScreener
    return pools


# ── Декодеры ──────────────────────────────────────────────────────────────────

def decode_whirlpool_price(data: bytes, pool: Pool) -> float | None:
    """Возвращает цену 1 target token в quote token."""
    if data is None or len(data) != WP_DATA_LEN:
        return None
    sqrt_price = int.from_bytes(data[WP_SQRT_PRICE:WP_SQRT_PRICE + 16], "little")
    if sqrt_price == 0:
        return None
    raw = (sqrt_price / (2**64)) ** 2  # = amount_b / amount_a в raw units

    if pool.target_is_a:
        # raw = quote / target → цена = raw * 10^(target_dec - quote_dec)
        return raw * 10 ** (pool.target_decimals - pool.quote_decimals)
    else:
        # raw = target / quote → цена = 10^(target_dec - quote_dec) / raw
        if raw == 0:
            return None
        return 10 ** (pool.target_decimals - pool.quote_decimals) / raw


def decode_raydium_price(base_data: bytes | None, quote_data: bytes | None,
                         pool: Pool) -> float | None:
    if base_data is None or quote_data is None:
        return None
    if len(base_data) < SPL_AMOUNT_OFFSET + 8 or len(quote_data) < SPL_AMOUNT_OFFSET + 8:
        return None
    base_amt = int.from_bytes(base_data[SPL_AMOUNT_OFFSET:SPL_AMOUNT_OFFSET + 8], "little")
    quote_amt = int.from_bytes(quote_data[SPL_AMOUNT_OFFSET:SPL_AMOUNT_OFFSET + 8], "little")
    if base_amt == 0 or quote_amt == 0:
        return None

    if pool.target_is_base:
        # base vault = target, quote vault = USDC/SOL
        return (quote_amt / 10 ** pool.quote_decimals) / (base_amt / 10 ** pool.target_decimals)
    else:
        # base vault = USDC/SOL, quote vault = target
        return (base_amt / 10 ** pool.quote_decimals) / (quote_amt / 10 ** pool.target_decimals)


# ── Tick loop ─────────────────────────────────────────────────────────────────

def _build_address_list(pools: list[Pool]) -> tuple[list[str], list[tuple[str, int]]]:
    """Возвращает (адреса для batch RPC, индексы для парсинга обратно)."""
    addrs: list[str] = []
    layout: list[tuple[str, int]] = []
    for pool in pools:
        if pool.dex == "orca-whirlpool":
            layout.append(("orca", len(addrs)))
            addrs.append(pool.pool_address)
        elif pool.dex == "raydium-amm":
            layout.append(("raydium", len(addrs)))
            addrs.append(pool.base_vault)
            addrs.append(pool.quote_vault)
    return addrs, layout


def tick_prices(rpc: str, pools: list[Pool],
                addrs: list[str], layout: list[tuple[str, int]]) -> dict[int, float]:
    """Возвращает {pool_idx: price_in_quote}."""
    accounts = get_multiple_accounts(rpc, addrs)
    prices: dict[int, float] = {}
    for i, (pool, (kind, start)) in enumerate(zip(pools, layout)):
        if kind == "orca":
            price = decode_whirlpool_price(accounts[start], pool)
        elif kind == "raydium":
            price = decode_raydium_price(accounts[start], accounts[start + 1], pool)
        else:
            price = None
        if price is not None and price > 0:
            prices[i] = price
    return prices


def detect_arbs(pools: list[Pool], prices: dict[int, float]) -> list[dict]:
    """Группирует пулы по (symbol, quote_mint), ищет cross-DEX спреды."""
    by_group: dict[tuple[str, str], list[tuple[int, Pool, float]]] = {}
    for i, pool in enumerate(pools):
        if i in prices:
            by_group.setdefault((pool.symbol, pool.quote_mint), []).append((i, pool, prices[i]))

    opps = []
    ts = datetime.now(timezone.utc).isoformat()
    for (symbol, quote_mint), group in by_group.items():
        if len(group) < 2:
            continue
        quote_label = "USDC" if quote_mint == USDC_MINT else "SOL"
        for _, buy_pool, buy_price in group:
            for _, sell_pool, sell_price in group:
                if buy_pool.dex == sell_pool.dex:
                    continue
                if sell_price <= buy_price:
                    continue
                gross = (sell_price / buy_price - 1) * 100
                if gross > MAX_GROSS_SPREAD_PCT:
                    continue
                fee = buy_pool.fee_pct + sell_pool.fee_pct
                net = gross - fee
                if net < MIN_NET_SPREAD_PCT:
                    continue
                opps.append({
                    "ts": ts,
                    "symbol": symbol,
                    "quote": quote_label,
                    "buy_dex": buy_pool.dex,
                    "sell_dex": sell_pool.dex,
                    "buy_price": round(buy_price, 10),
                    "sell_price": round(sell_price, 10),
                    "gross_pct": round(gross, 4),
                    "fee_pct": round(fee, 4),
                    "net_pct": round(net, 4),
                    "profit_on_20_usd": round(20 * net / 100, 4),
                    "buy_pool": buy_pool.pool_address[:8],
                    "sell_pool": sell_pool.pool_address[:8],
                })
    return opps


# ── Run ───────────────────────────────────────────────────────────────────────

def run(duration: int, tokens: list[tuple[str, str]], interval: float) -> None:
    rpc = _rpc_url()
    log.info("=== Discovery (%d токенов) ===", len(tokens))
    pools = discover_pools(tokens, rpc)

    if not pools:
        log.error("Нет пулов — нечего мониторить.")
        return

    # Группировка для отчёта о coverage
    by_symbol: dict[str, list[str]] = {}
    for p in pools:
        by_symbol.setdefault(p.symbol, []).append(p.dex)

    log.info("=== Coverage ===")
    cross_dex = []
    for sym, _ in tokens:
        dexes = by_symbol.get(sym, [])
        status = "✓ cross-DEX" if len(set(dexes)) >= 2 else ("~ single DEX" if dexes else "✗ no pools")
        log.info("  %-10s %d пулов  %s  [%s]", sym, len(dexes), status, ",".join(dexes) or "—")
        if len(set(dexes)) >= 2:
            cross_dex.append(sym)

    log.info("Cross-DEX comparable: %d / %d токенов", len(cross_dex), len(tokens))
    if not cross_dex:
        log.warning("Ни один токен не имеет пулов на >=2 DEX'ах — арбитраж невозможен.")
        return

    if not OUTPUT_CSV.exists():
        with OUTPUT_CSV.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

    addrs, layout = _build_address_list(pools)
    log.info("=== Старт замера: %ds, interval=%.2fs, batched %d accounts/tick ===",
             duration, interval, len(addrs))
    log.info("CSV → %s", OUTPUT_CSV.resolve())

    start = time.time()
    tick_count = 0
    total_opps = 0
    max_net_by_symbol: dict[str, float] = {}

    try:
        while time.time() - start < duration:
            tick_start = time.time()
            prices = tick_prices(rpc, pools, addrs, layout)
            opps = detect_arbs(pools, prices)

            if opps:
                with OUTPUT_CSV.open("a", newline="") as f:
                    csv.DictWriter(f, fieldnames=CSV_FIELDS).writerows(opps)
                total_opps += len(opps)
                for o in opps:
                    if o["net_pct"] > max_net_by_symbol.get(o["symbol"], 0):
                        max_net_by_symbol[o["symbol"]] = o["net_pct"]
                top = max(opps, key=lambda o: o["net_pct"])
                print(f"  t={int(tick_start-start):4d}s  ARB {top['symbol']:10s} "
                      f"{top['buy_dex']:14s}→{top['sell_dex']:14s} "
                      f"gross={top['gross_pct']:.3f}%  NET={top['net_pct']:+.3f}%  "
                      f"(x{len(opps)})")

            tick_count += 1
            if tick_count % 30 == 0:
                pools_with_price = len(prices)
                print(f"  ··· t={int(tick_start-start):4d}s  ticks={tick_count}  "
                      f"active_pools={pools_with_price}/{len(pools)}  total_arbs={total_opps}")

            elapsed = time.time() - tick_start
            if elapsed < interval:
                time.sleep(interval - elapsed)
    except KeyboardInterrupt:
        log.info("Прервано пользователем")

    print_summary(pools, tick_count, total_opps, max_net_by_symbol,
                  time.time() - start, cross_dex)


def print_summary(pools, ticks, total_opps, max_net, elapsed, cross_dex_tokens) -> None:
    print("\n" + "=" * 70)
    print("ИТОГИ")
    print("=" * 70)
    print(f"Длительность: {elapsed:.1f}s  тиков: {ticks}  факт. частота: {ticks/elapsed:.2f} Hz")
    print(f"Пулов отслеживалось: {len(pools)}  cross-DEX токенов: {len(cross_dex_tokens)}")
    print(f"Всего opportunities (net > {MIN_NET_SPREAD_PCT}%): {total_opps}")

    if max_net:
        print(f"\nМаксимальный NET спред по токенам:")
        for sym, net in sorted(max_net.items(), key=lambda x: -x[1]):
            n_per_min = sum(1 for v in [net] if v) / max(elapsed / 60, 1)
            print(f"  {sym:10s} max NET = {net:+.3f}%")
    else:
        print(f"\n✗ Ни одной арбитражной возможности не зафиксировано за {elapsed:.0f}s.")
        print(f"  Для cross-DEX токенов ({', '.join(cross_dex_tokens)}) edge'а нет")
        print(f"  на разрешении {int(elapsed/max(ticks,1)*1000)}ms.")


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description="Realtime cross-DEX арбитраж scanner")
    parser.add_argument("--duration", type=int, default=300, help="Длительность, сек")
    parser.add_argument("--interval", type=float, default=1.0, help="Тик-интервал, сек")
    parser.add_argument("--tokens", help="comma-separated mints (override default list)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.tokens:
        tokens = [(m.strip()[:8].upper(), m.strip()) for m in args.tokens.split(",") if m.strip()]
    else:
        tokens = DEFAULT_TOKENS

    run(args.duration, tokens, args.interval)
