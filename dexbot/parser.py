"""Extract swap events from Helius enhanced transactions.

Pure functions — no I/O. Fully unit-testable against fixture JSON.
"""
from __future__ import annotations

from dataclasses import dataclass

# Solana mints we treat as "quote" (numeraire), not as the asset being traded.
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({SOL_MINT, USDC_MINT, USDT_MINT})

LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class SwapEvent:
    signature: str
    timestamp: int                 # unix seconds
    wallet: str
    action: str                    # 'buy' | 'sell'
    token_mint: str
    token_amount: float
    quote_mint: str
    quote_amount: float            # in quote currency units (SOL, USDC, USDT)
    sol_amount: float | None       # in SOL if quote was SOL, else None
    source: str | None             # DEX router, if Helius identified it


def _sum_token_changes(transfers: list[dict], wallet: str) -> dict[str, float]:
    """Net token delta per mint for `wallet`. Positive = received, negative = sent."""
    deltas: dict[str, float] = {}
    for t in transfers:
        mint = t.get("mint")
        if not mint:
            continue
        amt = t.get("tokenAmount") or 0
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            continue
        if t.get("toUserAccount") == wallet:
            deltas[mint] = deltas.get(mint, 0.0) + amt
        elif t.get("fromUserAccount") == wallet:
            deltas[mint] = deltas.get(mint, 0.0) - amt
    return deltas


def _net_native_lamports(transfers: list[dict], wallet: str) -> int:
    """Net SOL delta in lamports for `wallet`."""
    net = 0
    for t in transfers:
        amt = t.get("amount") or 0
        try:
            amt = int(amt)
        except (TypeError, ValueError):
            continue
        if t.get("toUserAccount") == wallet:
            net += amt
        elif t.get("fromUserAccount") == wallet:
            net -= amt
    return net


def parse_swap(tx: dict, wallet: str) -> SwapEvent | None:
    """Return a SwapEvent if this tx is a swap involving `wallet` between a
    quote mint and exactly one non-quote token. Otherwise None.

    Handles two shapes:
      1. Native+token transfers (most common with Jupiter/Raydium).
      2. events.swap structured payload when Helius parsed it cleanly.
    """
    if tx.get("type") not in ("SWAP", "TRANSFER", None) and not tx.get("events", {}).get("swap"):
        # Some Helius types we won't consider: STAKE, BURN, NFT_*, etc.
        if tx.get("type") not in ("UNKNOWN",):
            return None

    sig = tx.get("signature")
    ts = tx.get("timestamp")
    if not sig or not ts:
        return None

    token_deltas = _sum_token_changes(tx.get("tokenTransfers") or [], wallet)
    native_lamports = _net_native_lamports(tx.get("nativeTransfers") or [], wallet)

    # Drop dust deltas (e.g. SPL token rent / fees can leave tiny residues).
    token_deltas = {m: a for m, a in token_deltas.items() if abs(a) > 1e-9}

    non_quote = {m: a for m, a in token_deltas.items() if m not in QUOTE_MINTS}
    quote_token = {m: a for m, a in token_deltas.items() if m in QUOTE_MINTS}

    if len(non_quote) != 1:
        return None  # not a single-token swap

    target_mint, target_delta = next(iter(non_quote.items()))

    # Determine quote side. Prefer USDC/USDT over native SOL when both present.
    quote_mint: str
    quote_amount: float
    sol_amount: float | None = None
    if quote_token:
        quote_mint = next(iter(quote_token))
        # Buy: target_delta > 0 means we received memecoin → spent quote (negative)
        quote_amount = abs(quote_token[quote_mint])
        if quote_mint == SOL_MINT:
            sol_amount = quote_amount
    elif native_lamports != 0:
        quote_mint = SOL_MINT
        sol_amount = abs(native_lamports) / LAMPORTS_PER_SOL
        quote_amount = sol_amount
    else:
        return None  # no quote leg detected

    if target_delta > 0 and (quote_token.get(quote_mint, 0) < 0 or native_lamports < 0):
        action = "buy"
    elif target_delta < 0 and (quote_token.get(quote_mint, 0) > 0 or native_lamports > 0):
        action = "sell"
    else:
        return None  # ambiguous direction

    source = tx.get("source")  # Helius fills this with DEX router name when known

    return SwapEvent(
        signature=sig,
        timestamp=int(ts),
        wallet=wallet,
        action=action,
        token_mint=target_mint,
        token_amount=abs(target_delta),
        quote_mint=quote_mint,
        quote_amount=float(quote_amount),
        sol_amount=sol_amount,
        source=source,
    )


def parse_swaps(transactions: list[dict], wallet: str) -> list[SwapEvent]:
    out: list[SwapEvent] = []
    for tx in transactions:
        ev = parse_swap(tx, wallet)
        if ev is not None:
            out.append(ev)
    return out
