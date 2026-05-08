"""Helius Enhanced Transactions API client.

Docs: https://docs.helius.dev/api-reference/enhanced-transactions-api/parsed-transaction-history
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

BASE = "https://api.helius.xyz"
DEFAULT_TIMEOUT = 30
PAGE_LIMIT = 100  # API max per page


class HeliusError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise HeliusError("HELIUS_API_KEY env var not set")
    return key


def fetch_address_transactions(
    address: str,
    *,
    limit: int = PAGE_LIMIT,
    before: str | None = None,
) -> list[dict]:
    """One page of parsed transactions for a Solana address. Newest first."""
    params: dict[str, object] = {
        "api-key": _api_key(),
        "limit": min(limit, PAGE_LIMIT),
    }
    if before:
        params["before"] = before

    url = f"{BASE}/v0/addresses/{address}/transactions"
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            if attempt < 2:
                time.sleep(1 + attempt)
    raise HeliusError(f"Helius GET {url} failed: {last_exc}")


def fetch_transactions_since(
    address: str,
    *,
    since_unix: int,
    max_pages: int = 10,
) -> list[dict]:
    """Paginate until oldest tx is older than `since_unix` (or pages exhausted).

    Returns transactions newer than `since_unix`, newest first. Cap at
    `max_pages * PAGE_LIMIT` transactions to keep API budget bounded —
    if a wallet trades more than that in the window, we still get the most
    recent slice, which is what scoring cares about.
    """
    out: list[dict] = []
    before: str | None = None

    for _ in range(max_pages):
        batch = fetch_address_transactions(address, before=before)
        if not batch:
            break
        out.extend(batch)
        oldest_ts = batch[-1].get("timestamp", 0)
        if oldest_ts and oldest_ts < since_unix:
            break
        sig = batch[-1].get("signature")
        if not sig:
            break
        before = sig

    return [tx for tx in out if (tx.get("timestamp") or 0) >= since_unix]
