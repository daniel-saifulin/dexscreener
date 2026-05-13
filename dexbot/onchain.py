"""On-chain метрики для гипотезы B: pool/token creation timestamps.

Источники в порядке предпочтения:
  1. DexScreener `pairCreatedAt` — мгновенный, бесплатный, покрывает 99% индексированных токенов.
  2. Helius `getSignaturesForAddress` — fallback для свежих токенов (секунды/минуты от создания)
     которые DexScreener ещё не успел проиндексировать.

Все запросы проходят через персистентный кэш (`pool_metadata` таблица) — один уникальный
адрес опрашиваем максимум один раз. Это критично для бэкфилла где могут быть тысячи unique mints.

Helper'ы:
  fetch_pool_age_min_at(token_mint, signal_ts, conn) -> int | None
  pool_address_from_tx(tx) -> str | None     # best-effort extraction
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

DEXSCREENER_TIMEOUT = 10
HELIUS_TIMEOUT = 30

# In-memory L1 cache: address -> creation_ts (unix seconds) or None if known-unknown.
# Сбрасывается при перезапуске процесса; персистентный L2 — в pool_metadata.
_L1_CACHE: dict[str, int | None] = {}


def _helius_rpc_url() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY not set")
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


# ---------------------------------------------------------------------------
# DexScreener path — primary
# ---------------------------------------------------------------------------

def _dexscreener_pair_created_ts(token_mint: str) -> int | None:
    """Возвращает unix-секунды самого раннего пула на DexScreener для этого токена."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
            timeout=DEXSCREENER_TIMEOUT,
        )
        if not r.ok:
            return None
        data = r.json()
        pairs = (data.get("pairs") if isinstance(data, dict) else None) or []
        # На случай мульти-чейн листинга — берём только Solana пары
        sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
        candidates = sol_pairs or pairs
        valid = [p.get("pairCreatedAt") for p in candidates if p.get("pairCreatedAt")]
        if not valid:
            return None
        # pairCreatedAt в миллисекундах; берём самый ранний пул для токена
        return int(min(int(v) for v in valid) / 1000)
    except Exception as e:
        log.debug("dexscreener lookup failed for %s: %s", token_mint[:8], e)
        return None


# ---------------------------------------------------------------------------
# Helius path — fallback
# ---------------------------------------------------------------------------

def _helius_oldest_signature_ts(address: str, max_pages: int = 5) -> int | None:
    """Пагинирует `getSignaturesForAddress` назад до самой ранней транзакции.

    max_pages=5 даёт нам 5000 транзакций максимум. Для свежих токенов этого хватит.
    Для очень активных старых mints мы можем не дойти до самой первой за 5 страниц —
    в таком случае вернём None и оставим creation_ts NULL.
    """
    url = _helius_rpc_url()
    before = None
    oldest_ts: int | None = None
    pages = 0

    while pages < max_pages:
        try:
            params: list[Any] = [address, {"limit": 1000}]
            if before:
                params[1]["before"] = before
            r = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": params},
                timeout=HELIUS_TIMEOUT,
            )
            r.raise_for_status()
            result = r.json().get("result") or []
            if not result:
                break
            oldest_ts = result[-1].get("blockTime") or oldest_ts
            if len(result) < 1000:
                # Дошли до самой старой
                break
            before = result[-1].get("signature")
            pages += 1
            time.sleep(0.1)  # gentle rate-limit
        except Exception as e:
            log.warning("helius getSignaturesForAddress failed for %s: %s", address[:8], e)
            return None

    return oldest_ts


# ---------------------------------------------------------------------------
# Cached resolver
# ---------------------------------------------------------------------------

def _load_from_db(conn, address: str) -> tuple[bool, int | None]:
    """Returns (found_in_cache, creation_ts_or_None)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT creation_ts FROM pool_metadata WHERE address=%s",
            (address,),
        )
        row = cur.fetchone()
        if row is None:
            return False, None
        if row[0] is None:
            return True, None
        return True, int(row[0].timestamp())


def _save_to_db(conn, address: str, creation_ts: int | None, source: str,
                error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pool_metadata (address, creation_ts, source, error)
            VALUES (%s, to_timestamp(%s), %s, %s)
            ON CONFLICT (address) DO UPDATE
              SET creation_ts = EXCLUDED.creation_ts,
                  source      = EXCLUDED.source,
                  error       = EXCLUDED.error
            """,
            (address, creation_ts, source, error),
        )


def resolve_creation_ts(address: str, conn=None) -> int | None:
    """Returns unix-seconds of address creation, or None if unknown.

    Используется кэширование на двух уровнях: in-memory + DB (если conn передан).
    Lookup order:  L1 → L2 (DB) → DexScreener → Helius → write back to caches.
    """
    if address in _L1_CACHE:
        return _L1_CACHE[address]

    if conn is not None:
        found, ts = _load_from_db(conn, address)
        if found:
            _L1_CACHE[address] = ts
            return ts

    # Primary: DexScreener
    ts = _dexscreener_pair_created_ts(address)
    source = "dexscreener"

    if ts is None:
        # Fallback: Helius
        try:
            ts = _helius_oldest_signature_ts(address)
            source = "helius" if ts is not None else "helius_miss"
        except Exception as e:
            ts = None
            source = "error"
            log.warning("helius fallback failed for %s: %s", address[:8], e)

    _L1_CACHE[address] = ts
    if conn is not None:
        _save_to_db(conn, address, ts, source)

    return ts


def fetch_pool_age_min_at(token_mint: str, signal_ts: int, conn=None) -> int | None:
    """Главный API: возраст токена/пула в минутах на момент signal_ts."""
    creation_ts = resolve_creation_ts(token_mint, conn=conn)
    if creation_ts is None:
        return None
    age_sec = signal_ts - creation_ts
    if age_sec < 0:
        return 0
    return int(age_sec / 60)


# ---------------------------------------------------------------------------
# Best-effort pool_address extraction from Helius enhanced tx
# ---------------------------------------------------------------------------

def pool_address_from_tx(tx: dict) -> str | None:
    """Best-effort извлечение pool/program адреса из enhanced swap tx.

    Helius enhanced shape:
      events.swap.innerSwaps[i].programInfo.{source|account}
      events.swap.tokenInputs[i].{fromTokenAccount|userAccount}

    Возвращаем первый ненулевой кандидат. Не гарантируется что это именно AMM pool —
    может быть program account. Достаточно для grouping в backfill анализе.
    """
    events = (tx.get("events") or {}) if isinstance(tx, dict) else {}
    swap = events.get("swap") if isinstance(events, dict) else None
    if not isinstance(swap, dict):
        return None

    # Path 1: innerSwaps -> programInfo
    for inner in swap.get("innerSwaps") or []:
        if not isinstance(inner, dict):
            continue
        prog = inner.get("programInfo") or {}
        if isinstance(prog, dict):
            acc = prog.get("account") or prog.get("source") or prog.get("address")
            if acc:
                return acc

    # Path 2: top-level source
    src = swap.get("source") or tx.get("source")
    if src and len(str(src)) > 20:  # heuristic: tx.source is usually "RAYDIUM" not address
        return src

    return None
