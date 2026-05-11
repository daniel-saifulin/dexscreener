"""FastAPI webhook receiver for Helius enhanced transaction events.

Deployed on fly.io to give core-conviction trades sub-15-second latency
instead of the 10-minute cron polling baseline. Both paths coexist:
- This webhook handles core-wallet swap events as they happen.
- The GH Actions watcher cron remains running as a fallback (its dedup
  via `has_recent_open_or_closed_trade` prevents duplicate trades).

Endpoints:
  GET  /health           — fly.io healthcheck
  POST /webhook/helius   — Helius enhanced webhook payload

Environment:
  DATABASE_URL           — Neon Postgres
  HELIUS_API_KEY         — for the rare DexScreener-fail fallback
  HELIUS_WEBHOOK_SECRET  — optional shared secret (Helius can include it
                           in `Authorization` header for verification)
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Optional

import psycopg
import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# Загружаем .env для локальной разработки. На fly.io переменные приходят из секретов.
load_dotenv()

from .parser import parse_swap  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s webhook: %(message)s",
)
log = logging.getLogger("webhook")

HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET")


def _database_url() -> str | None:
    """Read at call-time, not import-time — supports late env-var injection."""
    return os.environ.get("DATABASE_URL")

MIN_CORE_BUYERS = 2
CORE_CONVICTION_WINDOW_MIN = 30
DEDUP_RECENT_TRADE_HOURS = 24
TP_PCT = 18.0
SL_PCT = -12.0
POLL_LOOKBACK_HOURS = 1  # ignore events older than this

app = FastAPI(title="dexbot-webhook")


# ---------------------------------------------------------------------------
# DB helpers (mirror watcher.py logic; copied here to avoid a heavy import)
# ---------------------------------------------------------------------------

@contextmanager
def _conn():
    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg.connect(url, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def fetch_core_set(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT address FROM watched_wallets WHERE is_core=TRUE AND is_active=TRUE"
        )
        return {row[0] for row in cur.fetchall()}


def count_core_buyers(conn, token_address: str, core_set: set[str],
                      window_min: int = CORE_CONVICTION_WINDOW_MIN) -> int:
    if not core_set:
        return 0
    placeholders = ",".join(["%s"] * len(core_set))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT wallet)
            FROM wallet_signals
            WHERE action='buy' AND chain='solana' AND token_address=%s
              AND wallet IN ({placeholders})
              AND block_time >= NOW() - INTERVAL '{window_min} minutes'
            """,
            (token_address, *core_set),
        )
        return cur.fetchone()[0] or 0


def has_recent_trade(conn, token_address: str,
                     hours: int = DEDUP_RECENT_TRADE_HOURS) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM wallet_paper_trades
            WHERE chain='solana' AND token_address=%s
              AND opened_at >= NOW() - (%s || ' hours')::interval
            LIMIT 1
            """,
            (token_address, str(hours)),
        )
        return cur.fetchone() is not None


def token_metadata(conn, addr: str) -> tuple[bool | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT BOOL_OR(passed_filters), MAX(symbol)
            FROM candidates
            WHERE chain='solana' AND token_address=%s
            """,
            (addr,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            return bool(row[0]), row[1]
        return None, None


def insert_signal(conn, *, wallet: str, ev, ever_passed: bool | None,
                  in_candidates: bool, symbol: str | None) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wallet_signals (
                wallet, signature, block_time, action, chain, token_address,
                token_symbol, token_amount, quote_mint, quote_amount, sol_amount,
                candidate_passed, in_candidates, raw
            )
            VALUES (%s, %s, to_timestamp(%s), %s, 'solana', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (wallet, signature, action, token_address) DO NOTHING
            RETURNING id
            """,
            (
                wallet, ev.signature, ev.timestamp, ev.action, ev.token_mint, symbol,
                ev.token_amount, ev.quote_mint, ev.quote_amount, ev.sol_amount,
                ever_passed, in_candidates,
                json.dumps({"source": ev.source, "via": "webhook"}),
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None


def open_paper_trade(conn, *, signal_id: int, token_address: str,
                     symbol: str | None, wallet: str,
                     entry_price: float) -> int | None:
    if entry_price <= 0:
        return None
    stop = entry_price * (1 + SL_PCT / 100.0)
    take = entry_price * (1 + TP_PCT / 100.0)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wallet_paper_trades (
                signal_id, chain, token_address, symbol, triggered_by_wallet,
                entry_price_usd, stop_price_usd, take_price_usd,
                from_core_conviction, from_webhook
            )
            VALUES (%s, 'solana', %s, %s, %s, %s, %s, %s, TRUE, TRUE)
            ON CONFLICT (signal_id) DO NOTHING
            RETURNING id
            """,
            (signal_id, token_address, symbol, wallet, entry_price, stop, take),
        )
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# DexScreener price (synchronous; ~200-500ms typical)
# ---------------------------------------------------------------------------

def fetch_sol_price(token_address: str) -> tuple[float | None, str | None]:
    """Returns (priceUsd, symbol) for a Solana token. None on failure."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=5,
        )
        if not r.ok:
            return None, None
        data = r.json()
        pairs = (data.get("pairs") if isinstance(data, dict) else None) or []
        pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
        if not pairs:
            return None, None
        # Best = highest liquidity
        valid = [p for p in pairs if (p.get("liquidity") or {}).get("usd")]
        if not valid:
            return None, None
        pair = max(valid, key=lambda p: float(p["liquidity"]["usd"]))
        price_str = pair.get("priceUsd")
        symbol = (pair.get("baseToken") or {}).get("symbol")
        return (float(price_str) if price_str else None), symbol
    except Exception as e:
        log.warning("dexscreener failed for %s: %s", token_address[:8], e)
        return None, None


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------

def identify_wallet(tx: dict, core_set: set[str]) -> str | None:
    """Find which of our watched wallets initiated this tx."""
    fp = tx.get("feePayer")
    if fp in core_set:
        return fp
    for t in (tx.get("tokenTransfers") or []):
        for k in ("fromUserAccount", "toUserAccount"):
            addr = t.get(k)
            if addr in core_set:
                return addr
    return None


def process_one_tx(conn, tx: dict, core_set: set[str]) -> dict[str, Any]:
    """Process a single Helius enhanced tx. Returns a status dict for logging."""
    sig = tx.get("signature", "?")[:12]
    ts = tx.get("timestamp", 0)
    if ts < time.time() - POLL_LOOKBACK_HOURS * 3600:
        return {"sig": sig, "skip": "too_old"}

    wallet = identify_wallet(tx, core_set)
    if wallet is None:
        return {"sig": sig, "skip": "not_our_wallet"}

    ev = parse_swap(tx, wallet)
    if ev is None:
        return {"sig": sig, "skip": "not_a_swap"}
    if ev.action != "buy":
        return {"sig": sig, "skip": f"action_{ev.action}"}

    ever_passed, cand_symbol = token_metadata(conn, ev.token_mint)
    in_cand = ever_passed is not None

    price, pair_symbol = fetch_sol_price(ev.token_mint)
    symbol = cand_symbol or pair_symbol

    sig_id = insert_signal(
        conn, wallet=wallet, ev=ev,
        ever_passed=ever_passed, in_candidates=in_cand, symbol=symbol,
    )
    if sig_id is None:
        conn.commit()
        return {"sig": sig, "skip": "signal_dup", "wallet": wallet[:8]}

    n_buyers = count_core_buyers(conn, ev.token_mint, core_set)
    result: dict[str, Any] = {
        "sig": sig, "wallet": wallet[:8], "token": (symbol or ev.token_mint[:8]),
        "n_core_buyers": n_buyers, "signal_id": sig_id,
    }

    if n_buyers < MIN_CORE_BUYERS:
        conn.commit()
        result["skip"] = f"only_{n_buyers}_core_buyers"
        return result

    if has_recent_trade(conn, ev.token_mint):
        conn.commit()
        result["skip"] = "dedup"
        return result

    if price is None:
        conn.commit()
        result["skip"] = "no_price"
        return result

    trade_id = open_paper_trade(
        conn, signal_id=sig_id, token_address=ev.token_mint,
        symbol=symbol, wallet=wallet, entry_price=price,
    )
    conn.commit()
    result["trade_id"] = trade_id
    result["entry_price"] = price
    return result


def process_payload(payload: Any) -> None:
    """Background-runnable processor. Acceptes Helius webhook payload shape."""
    if not isinstance(payload, list):
        log.warning("payload not a list: %s", type(payload).__name__)
        return
    log.info("processing %d events", len(payload))
    try:
        with _conn() as conn:
            core_set = fetch_core_set(conn)
            if not core_set:
                log.warning("core set empty — nothing to do")
                return
            for tx in payload:
                if not isinstance(tx, dict):
                    continue
                try:
                    result = process_one_tx(conn, tx, core_set)
                    if "trade_id" in result and result["trade_id"]:
                        log.info("  OPENED %s wallet=%s token=%s entry=%.6f core_buyers=%d",
                                 result["sig"], result["wallet"], result["token"],
                                 result["entry_price"], result["n_core_buyers"])
                    elif "skip" in result:
                        log.info("  skip %s: %s", result["sig"], result["skip"])
                except Exception as e:
                    conn.rollback()
                    log.exception("error on tx %s: %s", tx.get("signature", "?")[:12], e)
    except Exception:
        log.exception("payload processing failed")


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/helius")
async def helius_webhook(
    request: Request,
    background: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
):
    if HELIUS_WEBHOOK_SECRET:
        if not authorization or authorization.strip() != HELIUS_WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="bad auth")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    background.add_task(process_payload, payload)
    return JSONResponse({"ok": True, "queued": True})


# Convenience: list current core wallets (for debugging the deployed app)
@app.get("/core")
def core():
    try:
        with _conn() as conn:
            core_set = fetch_core_set(conn)
        return {"count": len(core_set), "addresses": sorted(core_set)}
    except Exception as e:
        return {"error": str(e)}
