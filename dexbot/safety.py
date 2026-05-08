"""Token safety checks.

Solana  -> RugCheck (https://api.rugcheck.xyz, public, no key)
EVM     -> GoPlus Security (https://api.gopluslabs.io, public, rate-limited)

Returns a unified dict:
  {
    'is_honeypot': bool|None,
    'buy_tax': float|None,            # percent
    'sell_tax': float|None,           # percent
    'top10_holder_pct': float|None,   # percent
    'flags': list[str],               # human-readable concerns
    'source': 'rugcheck'|'goplus'|'unsupported',
  }
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15

# Maps DexScreener chainId -> GoPlus chain_id parameter.
# https://docs.gopluslabs.io/reference/api-overview
GOPLUS_CHAIN_IDS: dict[str, str] = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "base": "8453",
    "arbitrum": "42161",
    "optimism": "10",
    "avalanche": "43114",
    "fantom": "250",
}


def _get(url: str, *, params: dict | None = None, retries: int = 1) -> Any:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(1)
    log.warning("safety GET %s failed: %s", url, last_exc)
    return None


def _empty(source: str = "unsupported") -> dict:
    return {
        "is_honeypot": None,
        "buy_tax": None,
        "sell_tax": None,
        "top10_holder_pct": None,
        "flags": [],
        "source": source,
    }


def check_solana(mint: str) -> dict:
    data = _get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    if not data:
        return _empty("rugcheck")

    flags: list[str] = []
    risks = data.get("risks") or []
    for r in risks:
        name = r.get("name") or "risk"
        level = r.get("level") or ""
        flags.append(f"{name}:{level}".strip(":"))

    # RugCheck "score" lower-is-better in their docs; we surface risks list.
    # Heuristic flags surfaced for filter logic:
    is_honeypot = None
    high_concentration = any("holder" in (r.get("name") or "").lower() for r in risks)
    top10 = None
    # RugCheck summary may include topHolders.percent; fall back to best-effort.
    th = data.get("topHolders") or data.get("top_holders")
    if isinstance(th, list) and th:
        try:
            top10 = sum(float(h.get("pct") or h.get("percent") or 0) for h in th[:10])
        except (TypeError, ValueError):
            top10 = None

    return {
        "is_honeypot": is_honeypot,
        "buy_tax": None,
        "sell_tax": None,
        "top10_holder_pct": top10,
        "flags": flags,
        "source": "rugcheck",
        "_concentration_warning": high_concentration,
    }


def check_evm(chain: str, address: str) -> dict:
    chain_id = GOPLUS_CHAIN_IDS.get(chain)
    if not chain_id:
        return _empty("unsupported")

    addr = address.lower()
    data = _get(
        f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}",
        params={"contract_addresses": addr},
    )
    if not data or data.get("code") != 1:
        return _empty("goplus")

    result = (data.get("result") or {}).get(addr) or {}
    if not result:
        return _empty("goplus")

    def _flt(k: str) -> float | None:
        v = result.get(k)
        if v in (None, "", "0"):
            return 0.0 if v == "0" else None
        try:
            return float(v) * 100  # GoPlus returns decimals like "0.05"
        except (TypeError, ValueError):
            return None

    is_honeypot_raw = result.get("is_honeypot")
    is_honeypot = bool(int(is_honeypot_raw)) if is_honeypot_raw in ("0", "1") else None

    flags: list[str] = []
    for k in ("is_proxy", "is_blacklisted", "can_take_back_ownership",
             "owner_change_balance", "trading_cooldown", "transfer_pausable",
             "is_mintable", "is_anti_whale", "selfdestruct"):
        v = result.get(k)
        if v == "1":
            flags.append(k)

    top10 = None
    holders = result.get("holders") or []
    try:
        top10 = sum(float(h.get("percent") or 0) * 100 for h in holders[:10])
    except (TypeError, ValueError):
        pass

    return {
        "is_honeypot": is_honeypot,
        "buy_tax": _flt("buy_tax"),
        "sell_tax": _flt("sell_tax"),
        "top10_holder_pct": top10,
        "flags": flags,
        "source": "goplus",
    }


def check(chain: str, token_address: str) -> dict:
    chain = chain.lower()
    if chain == "solana":
        return check_solana(token_address)
    if chain in GOPLUS_CHAIN_IDS:
        return check_evm(chain, token_address)
    return _empty("unsupported")
