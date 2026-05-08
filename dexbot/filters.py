"""Pure filter logic. No I/O, fully unit-testable."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Config


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str]   # rejection reasons; empty if passed


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(d: dict, *path, default=None):
    cur: object = d
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    if cur is None:
        return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def evaluate_pair(pair: dict, config: Config, now_ms: int | None = None) -> FilterResult:
    """Apply hard filters to a DexScreener pair object. Returns reasons list
    on failure; empty list on pass.

    Does not include safety check — that runs separately and is appended
    by the screener orchestrator.
    """
    if now_ms is None:
        now_ms = _now_ms()
    reasons: list[str] = []

    chain = (pair.get("chainId") or "").lower()
    if chain not in config.chains:
        reasons.append(f"chain {chain!r} not in {sorted(config.chains)}")

    liq_usd = _safe_float(pair, "liquidity", "usd")
    if liq_usd is None:
        reasons.append("missing liquidity.usd")
    else:
        if liq_usd < config.min_liquidity_usd:
            reasons.append(f"liquidity ${liq_usd:.0f} < ${config.min_liquidity_usd:.0f}")
        if liq_usd > config.max_liquidity_usd:
            reasons.append(f"liquidity ${liq_usd:.0f} > ${config.max_liquidity_usd:.0f}")

    created_at = pair.get("pairCreatedAt")
    if created_at is None:
        reasons.append("missing pairCreatedAt")
    else:
        try:
            created_at = int(created_at)
        except (TypeError, ValueError):
            reasons.append("pairCreatedAt not numeric")
            created_at = None
    if created_at is not None:
        age_min = max(0, (now_ms - created_at) / 60_000)
        if age_min < config.min_age_minutes:
            reasons.append(f"age {age_min:.0f}m < {config.min_age_minutes}m")
        if age_min > config.max_age_hours * 60:
            reasons.append(f"age {age_min:.0f}m > {config.max_age_hours * 60}m")

    vol_h1 = _safe_float(pair, "volume", "h1") or 0.0
    if vol_h1 < config.min_volume_h1_usd:
        reasons.append(f"vol_h1 ${vol_h1:.0f} < ${config.min_volume_h1_usd:.0f}")

    chg_h1 = _safe_float(pair, "priceChange", "h1")
    if chg_h1 is not None:
        if chg_h1 < config.min_price_change_h1:
            reasons.append(f"chg_h1 {chg_h1:.1f}% < {config.min_price_change_h1}%")
        if chg_h1 > config.max_price_change_h1:
            reasons.append(f"chg_h1 {chg_h1:.1f}% > {config.max_price_change_h1}%")

    txns_h1 = pair.get("txns", {}).get("h1") if isinstance(pair.get("txns"), dict) else None
    if isinstance(txns_h1, dict):
        buys = int(txns_h1.get("buys") or 0)
        sells = int(txns_h1.get("sells") or 0)
        total = buys + sells
        if total >= 10:  # ratio meaningless on tiny samples
            ratio = buys / total
            if ratio < config.min_buy_ratio_h1:
                reasons.append(f"buy_ratio {ratio:.2f} < {config.min_buy_ratio_h1}")

    return FilterResult(passed=not reasons, reasons=reasons)


def evaluate_safety(safety: dict, config: Config) -> list[str]:
    """Returns list of safety-rejection reasons. Empty list = safe."""
    reasons: list[str] = []
    if safety.get("is_honeypot") is True:
        reasons.append("honeypot")
    sell_tax = safety.get("sell_tax")
    if sell_tax is not None and sell_tax > config.max_sell_tax:
        reasons.append(f"sell_tax {sell_tax:.1f}% > {config.max_sell_tax}%")
    buy_tax = safety.get("buy_tax")
    if buy_tax is not None and buy_tax > config.max_buy_tax:
        reasons.append(f"buy_tax {buy_tax:.1f}% > {config.max_buy_tax}%")
    top10 = safety.get("top10_holder_pct")
    if top10 is not None and top10 > config.max_top10_holder_pct:
        reasons.append(f"top10_holder {top10:.1f}% > {config.max_top10_holder_pct}%")
    high_concern_flags = {
        "is_proxy", "is_blacklisted", "can_take_back_ownership",
        "owner_change_balance", "selfdestruct", "is_mintable",
    }
    for f in safety.get("flags") or []:
        if f in high_concern_flags:
            reasons.append(f"flag:{f}")
    return reasons
