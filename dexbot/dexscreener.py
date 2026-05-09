"""DexScreener public API client. No key required.

Endpoints used:
  GET /token-profiles/latest/v1   -> recent token profiles
  GET /token-boosts/latest/v1     -> recently boosted tokens
  GET /latest/dex/tokens/{addr}   -> all pairs for a token
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
DEFAULT_TIMEOUT = 15  # seconds
RETRY_ON_429_SEC = 5


class DexScreenerError(RuntimeError):
    pass


def _get(path: str, *, params: dict | None = None, retries: int = 2) -> Any:
    url = f"{BASE}{path}"
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 429:
                time.sleep(RETRY_ON_429_SEC)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(1 + attempt)
            continue
    raise DexScreenerError(f"GET {url} failed: {last_exc}")


def fetch_latest_profiles() -> list[dict]:
    """Recently created token profiles. Returns list of dicts with at least
    chainId and tokenAddress."""
    data = _get("/token-profiles/latest/v1")
    return data if isinstance(data, list) else []


def fetch_latest_boosts() -> list[dict]:
    """Recently boosted tokens (paid promotion — proxy for active interest)."""
    data = _get("/token-boosts/latest/v1")
    return data if isinstance(data, list) else []


def fetch_pairs_for_token(token_address: str) -> list[dict]:
    """All pairs across all chains for given token address."""
    data = _get(f"/latest/dex/tokens/{token_address}")
    if isinstance(data, dict):
        return data.get("pairs") or []
    return []


BATCH_SIZE = 30  # DexScreener accepts up to 30 comma-separated addresses


def fetch_pairs_for_tokens(token_addresses: list[str]) -> dict[str, list[dict]]:
    """Batch variant: returns {token_address: [pair, ...]} for many tokens
    in one go (one HTTP per BATCH_SIZE tokens). Tokens with no listings
    are absent from the result.
    """
    out: dict[str, list[dict]] = {}
    if not token_addresses:
        return out
    seen = list(dict.fromkeys(token_addresses))
    for i in range(0, len(seen), BATCH_SIZE):
        batch = seen[i:i + BATCH_SIZE]
        joined = ",".join(batch)
        try:
            data = _get(f"/latest/dex/tokens/{joined}")
        except DexScreenerError as e:
            log.warning("batch fetch (%d tokens) failed: %s", len(batch), e)
            continue
        if not isinstance(data, dict):
            continue
        for pair in data.get("pairs") or []:
            addr = ((pair.get("baseToken") or {}).get("address") or "").strip()
            if addr:
                out.setdefault(addr, []).append(pair)
    return out


def best_pair(pairs: list[dict]) -> dict | None:
    """Pick highest-liquidity pair (most reliable price discovery)."""
    valid = [p for p in pairs if (p.get("liquidity") or {}).get("usd")]
    if not valid:
        return None
    return max(valid, key=lambda p: float(p["liquidity"]["usd"]))
