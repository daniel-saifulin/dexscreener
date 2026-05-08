"""Runtime config loaded from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    database_url: str | None
    chains: frozenset[str]
    min_liquidity_usd: float
    max_liquidity_usd: float
    min_age_minutes: int
    max_age_hours: int
    min_volume_h1_usd: float
    min_price_change_h1: float
    max_price_change_h1: float
    min_buy_ratio_h1: float
    max_sell_tax: float
    max_buy_tax: float
    max_top10_holder_pct: float
    log_level: str


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def load_config() -> Config:
    load_dotenv()
    chains_raw = os.environ.get("CHAINS", "solana,base,ethereum")
    chains = frozenset(c.strip().lower() for c in chains_raw.split(",") if c.strip())
    return Config(
        database_url=os.environ.get("DATABASE_URL") or None,
        chains=chains,
        min_liquidity_usd=_f("MIN_LIQUIDITY_USD", 30_000),
        max_liquidity_usd=_f("MAX_LIQUIDITY_USD", 2_000_000),
        min_age_minutes=_i("MIN_AGE_MINUTES", 30),
        max_age_hours=_i("MAX_AGE_HOURS", 24),
        min_volume_h1_usd=_f("MIN_VOLUME_H1_USD", 5_000),
        min_price_change_h1=_f("MIN_PRICE_CHANGE_H1", -10),
        max_price_change_h1=_f("MAX_PRICE_CHANGE_H1", 200),
        min_buy_ratio_h1=_f("MIN_BUY_RATIO_H1", 0.55),
        max_sell_tax=_f("MAX_SELL_TAX", 5),
        max_buy_tax=_f("MAX_BUY_TAX", 5),
        max_top10_holder_pct=_f("MAX_TOP10_HOLDER_PCT", 60),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
