#!/usr/bin/env python3
"""
Realtime измеритель арбитражного edge'а через Helius RPC.

Опрашивает on-chain pool accounts напрямую — без посредника-индексатора.
Источники:
  - Orca Whirlpool SOL/USDC: sqrt_price прямо в pool account (1 RPC/тик)
  - DexScreener: для сравнения, измеряем реальный лаг индексатора

Цель эксперимента: получить честный ответ — насколько "1-2% спреды" из
DexScreener-сканера реальны, а насколько артефакт лага. Если on-chain цена
прыгает на ±0.3% за 1 секунду, а DS остаётся на старом значении — мы
доказали что edge закрыт ботами до того как мы его увидели.

Запуск:
    python -m dexbot.arb_realtime                  # 5 минут (default)
    python -m dexbot.arb_realtime --duration 1800  # 30 минут
"""
from __future__ import annotations

import argparse
import base64
import csv
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# ── Pool: Orca Whirlpool SOL/USDC (high-liquidity, ~$28M TVL) ─────────────────
# Эмпирически token_a = SOL, token_b = USDC в этом пуле (проверено по цене)
ORCA_POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
TOKEN_A_DECIMALS = 9   # SOL
TOKEN_B_DECIMALS = 6   # USDC

# ── Whirlpool account layout ──────────────────────────────────────────────────
# 8 (discriminator) + 32 (whirlpools_config) + 1 (whirlpool_bump)
# + 2 (tick_spacing) + 2 (tick_spacing_seed) + 2 (fee_rate)
# + 2 (protocol_fee_rate) + 16 (liquidity) → 65, далее 16 байт sqrt_price (u128)
SQRT_PRICE_OFFSET = 65
SQRT_PRICE_LEN = 16

# ── Outputs ───────────────────────────────────────────────────────────────────
OUTPUT_CSV = Path("arb_realtime_log.csv")
CSV_FIELDS = [
    "ts", "onchain_price_usd", "ds_price_usd",
    "lag_bps", "tick_age_ms", "slot",
]

# ── Polling ───────────────────────────────────────────────────────────────────
TICK_INTERVAL_SEC = 0.5     # 2 Hz on-chain
DS_INTERVAL_SEC   = 10.0    # DexScreener раз в 10 сек (rate-limit-friendly)


# ── Helius RPC ────────────────────────────────────────────────────────────────

def _rpc_url() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY не задан в .env")
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


def fetch_account(rpc_url: str, address: str) -> tuple[bytes, int] | None:
    """Возвращает (raw_data, slot) для account или None при ошибке."""
    try:
        r = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [address, {"encoding": "base64", "commitment": "processed"}],
        }, timeout=5)
        r.raise_for_status()
        result = r.json().get("result")
        if not result or not result.get("value"):
            return None
        data_b64 = result["value"]["data"][0]
        slot = result.get("context", {}).get("slot", 0)
        return base64.b64decode(data_b64), slot
    except Exception as e:
        log.warning("RPC error: %s", e)
        return None


# ── Whirlpool decoder ─────────────────────────────────────────────────────────

def parse_whirlpool_price(data: bytes) -> float | None:
    """
    Декодирует sqrt_price из Orca Whirlpool аккаунта.
    Возвращает цену 1 SOL в USD.

    Whirlpool формула:
        raw_price = (sqrt_price / 2^64)^2 = amount_b / amount_a (в lamports/units)
        В нашем пуле: a=SOL (9 dec), b=USDC (6 dec)
        => raw_price = USDC_units / SOL_lamports
        => USD per SOL (real) = raw_price * 10^(decimals_a - decimals_b)
                              = raw_price * 10^3
    """
    if len(data) < SQRT_PRICE_OFFSET + SQRT_PRICE_LEN:
        return None
    sqrt_price_raw = int.from_bytes(
        data[SQRT_PRICE_OFFSET:SQRT_PRICE_OFFSET + SQRT_PRICE_LEN],
        "little",
    )
    if sqrt_price_raw == 0:
        return None
    raw_price = (sqrt_price_raw / (2**64)) ** 2
    decimal_adj = 10 ** (TOKEN_A_DECIMALS - TOKEN_B_DECIMALS)  # 10^3
    return raw_price * decimal_adj


# ── DexScreener (для сравнения) ───────────────────────────────────────────────

def fetch_ds_price(pool_address: str) -> float | None:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool_address}",
            timeout=8,
        )
        r.raise_for_status()
        pair = r.json().get("pair") or {}
        return float(pair.get("priceUsd") or 0) or None
    except Exception as e:
        log.warning("DexScreener error: %s", e)
        return None


# ── Main loop ─────────────────────────────────────────────────────────────────

def main(duration: int) -> None:
    rpc_url = _rpc_url()
    log.info("Realtime замер арбитражного лага — Orca Whirlpool SOL/USDC")
    log.info("Pool: %s", ORCA_POOL)
    log.info("Длительность: %d сек, тик %dms, DS раз в %.0fs",
             duration, int(TICK_INTERVAL_SEC * 1000), DS_INTERVAL_SEC)
    log.info("CSV → %s", OUTPUT_CSV.resolve())

    if not OUTPUT_CSV.exists():
        with OUTPUT_CSV.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

    start = time.time()
    last_ds_check = 0.0
    last_ds_price: float | None = None
    last_onchain_price: float | None = None
    onchain_history: list[tuple[float, float]] = []  # (ts, price)
    max_lag_bps = 0.0
    tick_count = 0
    last_print = start

    try:
        while time.time() - start < duration:
            tick_start = time.time()

            # On-chain тик
            account = fetch_account(rpc_url, ORCA_POOL)
            if account is None:
                time.sleep(1)
                continue
            data, slot = account
            onchain_price = parse_whirlpool_price(data)
            if onchain_price is None:
                time.sleep(1)
                continue

            tick_age_ms = int((time.time() - tick_start) * 1000)
            onchain_history.append((tick_start, onchain_price))
            last_onchain_price = onchain_price

            # DexScreener раз в N секунд
            if tick_start - last_ds_check >= DS_INTERVAL_SEC:
                ds_p = fetch_ds_price(ORCA_POOL)
                if ds_p:
                    last_ds_price = ds_p
                last_ds_check = tick_start

            # Лаг в bps
            if last_ds_price:
                lag_bps = abs(onchain_price - last_ds_price) / last_ds_price * 10000
                max_lag_bps = max(max_lag_bps, lag_bps)
            else:
                lag_bps = 0.0

            # Лог в CSV
            with OUTPUT_CSV.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "onchain_price_usd": round(onchain_price, 6),
                    "ds_price_usd": round(last_ds_price or 0, 6),
                    "lag_bps": round(lag_bps, 2),
                    "tick_age_ms": tick_age_ms,
                    "slot": slot,
                })

            tick_count += 1

            # Раз в 5 секунд печатаем сводку в консоль
            if tick_start - last_print >= 5:
                ds_str = f"${last_ds_price:.4f}" if last_ds_price else "—"
                print(
                    f"  t={int(tick_start-start):4d}s  on-chain=${onchain_price:.4f}  "
                    f"DS={ds_str}  lag={lag_bps:5.1f}bps  slot={slot}  ticks={tick_count}"
                )
                last_print = tick_start

            # Поддерживаем темп ~2 Hz
            elapsed = time.time() - tick_start
            if elapsed < TICK_INTERVAL_SEC:
                time.sleep(TICK_INTERVAL_SEC - elapsed)

    except KeyboardInterrupt:
        log.info("\nПрервано пользователем")

    _print_summary(onchain_history, max_lag_bps, tick_count, time.time() - start)


def _print_summary(history: list[tuple[float, float]], max_lag: float,
                   ticks: int, elapsed: float) -> None:
    print("\n" + "=" * 60)
    print("ИТОГИ")
    print("=" * 60)
    print(f"Время:  {elapsed:.1f}s  /  тиков: {ticks}  /  факт. частота: {ticks/elapsed:.2f} Hz")

    if not history:
        print("Нет данных.")
        return

    prices = [p for _, p in history]
    p_min, p_max, p_mid = min(prices), max(prices), sorted(prices)[len(prices)//2]
    p_range_bps = (p_max - p_min) / p_mid * 10000

    print(f"\nOn-chain цена SOL/USD:")
    print(f"  median = ${p_mid:.4f}")
    print(f"  диапазон = ${p_min:.4f} … ${p_max:.4f}  ({p_range_bps:.1f} bps = {p_range_bps/100:.3f}%)")

    # Максимальное движение за 1 секунду — proxy для скорости MEV-окна
    max_1s_move_bps = 0.0
    for i in range(1, len(history)):
        dt = history[i][0] - history[i-1][0]
        if 0.3 < dt < 1.5:  # примерно 1-секундный интервал
            dp_bps = abs(history[i][1] - history[i-1][1]) / history[i-1][1] * 10000
            max_1s_move_bps = max(max_1s_move_bps, dp_bps)

    print(f"  макс. движение за тик (~1s): {max_1s_move_bps:.1f} bps  ({max_1s_move_bps/100:.3f}%)")

    print(f"\nDexScreener vs on-chain:")
    print(f"  макс. lag за наблюдение: {max_lag:.1f} bps  ({max_lag/100:.3f}%)")

    print(f"\nЧто это значит:")
    if max_lag > 30:
        print(f"  ✗ DexScreener отстаёт до {max_lag/100:.2f}% — наш v1-сканер видит фантомные спреды")
    elif max_lag > 10:
        print(f"  ~ DexScreener иногда показывает stale данные ({max_lag/100:.2f}%), но не критично")
    else:
        print(f"  ✓ DexScreener-данные близки к on-chain — лаг ≤{max_lag/100:.2f}%")

    if p_range_bps > 30:
        print(f"  ✗ Цена дёргается на {p_range_bps/100:.2f}% за окно — высокая волатильность")
        print(f"    при таком движении 'арбитраж' между DEX'ами в индексаторе — это лаг, не edge")
    else:
        print(f"  ~ Цена стабильна (диапазон {p_range_bps/100:.2f}%)")

    print(f"\nДанные → {OUTPUT_CSV.resolve()}")


if __name__ == "__main__":
    load_dotenv()

    parser = argparse.ArgumentParser(description="Realtime замер арбитражного лага через Helius")
    parser.add_argument("--duration", type=int, default=300,
                        help="Длительность, сек (default: 300)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    main(args.duration)
