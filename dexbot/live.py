"""Live trading через Jupiter (Solana DEX aggregator).

Этот модуль реализует ТОЛЬКО механику swap'ов. Решение «открывать или нет»
делается выше по стеку: risk_guard + safety_runtime.

Зависит от:
- Jupiter Quote API: https://quote-api.jup.ag/v6/quote
- Jupiter Swap API: https://quote-api.jup.ag/v6/swap
- Solana RPC: используем Helius (мы платим за Developer tier)

Приватный ключ кошелька:
- ЛОКАЛЬНО: в .env как `SOLANA_PRIVATE_KEY` (base58)
- В ПРОДЕ (fly.io): через `fly secrets set SOLANA_PRIVATE_KEY=...`
- НИКОГДА в коде, никогда в git, никогда в логах.

CLI команды (для тестирования без реального ключа):
    python -m dexbot.live quote --token <mint> --amount-usd 5
        # Запрашивает quote от Jupiter, ничего не отправляет
    python -m dexbot.live balance
        # Показывает баланс кошелька (нужен LIVE_WALLET_ADDRESS env)
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
from typing import Optional

import requests

log = logging.getLogger("dexbot.live")

# Solana системные mints
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_SLIPPAGE_BPS = 300  # 3%

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"


def _helius_rpc_url() -> str:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY required for live trading")
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


# ---------------------------------------------------------------------------
# Jupiter Quote API
# ---------------------------------------------------------------------------

def get_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    *,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    """Запрашивает swap quote от Jupiter v6.

    Параметры:
      input_mint, output_mint: SPL mint addresses
      amount: сколько input-токенов в их smallest units (lamports для SOL)
      slippage_bps: допустимое проскальзывание в basis points (300 = 3%)

    Возвращает quote dict с полями inAmount, outAmount, priceImpactPct, routePlan и т.д.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": "false",
    }
    r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def estimate_swap_output(quote: dict) -> int:
    """Сколько output-токенов получим (в smallest units)."""
    return int(quote.get("outAmount", 0))


def estimate_price_impact_pct(quote: dict) -> float:
    """Price impact в процентах."""
    try:
        return float(quote.get("priceImpactPct", 0)) * 100.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Jupiter Swap API (готовит транзакцию для подписи)
# ---------------------------------------------------------------------------

def build_swap_transaction(quote: dict, user_pubkey: str) -> str:
    """Строит транзакцию для swap. Возвращает base64-encoded raw tx.

    Транзакцию ещё нужно подписать приватным ключом и отправить через RPC.
    """
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,        # автоматический wrap SOL <-> WSOL
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    r = requests.post(JUPITER_SWAP_URL, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data["swapTransaction"]


# ---------------------------------------------------------------------------
# Solana RPC
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list, *, timeout: int = 15) -> dict:
    url = _helius_rpc_url()
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(url, json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


def get_sol_balance(wallet_pubkey: str) -> int:
    """Возвращает balance в lamports."""
    result = _rpc("getBalance", [wallet_pubkey])
    return int(result.get("value", 0)) if isinstance(result, dict) else 0


def get_token_balance(wallet_pubkey: str, mint: str) -> int:
    """Возвращает баланс SPL-токена в smallest units."""
    result = _rpc(
        "getTokenAccountsByOwner",
        [wallet_pubkey, {"mint": mint},
         {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    if not result or not result.get("value"):
        return 0
    total = 0
    for acc in result["value"]:
        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        amount = info.get("tokenAmount", {}).get("amount")
        if amount:
            total += int(amount)
    return total


def send_signed_transaction(signed_tx_b64: str) -> str:
    """Отправляет уже подписанную base64 транзакцию. Returns tx signature."""
    return _rpc("sendTransaction",
                [signed_tx_b64, {"encoding": "base64", "skipPreflight": False,
                                 "maxRetries": 3}])


def wait_for_confirmation(signature: str, *, timeout_sec: int = 30) -> bool:
    """Поллим до подтверждения или timeout. Возвращает True если confirmed."""
    import time
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            result = _rpc("getSignatureStatuses", [[signature]])
            statuses = result.get("value") or []
            if statuses and statuses[0]:
                conf = statuses[0].get("confirmationStatus")
                if conf in ("confirmed", "finalized"):
                    return True
                if statuses[0].get("err"):
                    log.error("tx %s failed: %s", signature, statuses[0]["err"])
                    return False
        except Exception as e:
            log.warning("confirmation check err: %s", e)
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Подпись транзакции (требует solders)
# ---------------------------------------------------------------------------

def sign_transaction(tx_b64: str, secret_key_b58: str) -> str:
    """Подписывает unsigned transaction приватным ключом. Returns signed b64.

    Использует solders для криптографии. Импорт лениво — если живой ключ
    не нужен, модуль работает без solders.
    """
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        import base58
    except ImportError as e:
        raise RuntimeError(
            "live signing requires 'solders' and 'base58'. "
            "Add to requirements: solders>=0.20, base58>=2.1"
        ) from e

    keypair = Keypair.from_bytes(base58.b58decode(secret_key_b58))
    tx_bytes = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    signed = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def usd_to_sol_lamports(amount_usd: float, sol_price_usd: float) -> int:
    """Конвертирует $X в lamports SOL."""
    if sol_price_usd <= 0:
        raise ValueError("sol_price_usd must be > 0")
    sol_amount = amount_usd / sol_price_usd
    return int(sol_amount * LAMPORTS_PER_SOL)


def sol_lamports_to_usd(lamports: int, sol_price_usd: float) -> float:
    return (lamports / LAMPORTS_PER_SOL) * sol_price_usd


# ---------------------------------------------------------------------------
# CLI (для безопасного тестирования без выполнения реальных сделок)
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dexbot.live",
                                     description="Live trading helpers (read-only by default).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_quote = sub.add_parser("quote", help="Запрашивает Jupiter quote (read-only).")
    p_quote.add_argument("--token", required=True, help="output token mint")
    p_quote.add_argument("--amount-usd", type=float, default=5.0)
    p_quote.add_argument("--sol-price", type=float, default=200.0,
                         help="SOL/USD на момент конвертации")
    p_quote.add_argument("--slippage-bps", type=int, default=DEFAULT_SLIPPAGE_BPS)

    p_bal = sub.add_parser("balance", help="Показывает SOL баланс кошелька.")
    p_bal.add_argument("--wallet", help="public key; default: LIVE_WALLET_ADDRESS env")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "quote":
        lamports = usd_to_sol_lamports(args.amount_usd, args.sol_price)
        print(f"Quote SOL → {args.token[:8]}... amount={args.amount_usd}$ "
              f"({lamports} lamports)")
        try:
            q = get_quote(SOL_MINT, args.token, lamports, slippage_bps=args.slippage_bps)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"  outAmount      : {q.get('outAmount')}")
        print(f"  priceImpactPct : {estimate_price_impact_pct(q):.2f}%")
        print(f"  routeLabels    : {[step.get('swapInfo', {}).get('label') for step in q.get('routePlan', [])]}")
        return 0

    if args.cmd == "balance":
        wallet = args.wallet or os.environ.get("LIVE_WALLET_ADDRESS")
        if not wallet:
            print("ERROR: provide --wallet or set LIVE_WALLET_ADDRESS", file=sys.stderr)
            return 2
        try:
            lamports = get_sol_balance(wallet)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"Wallet {wallet[:8]}...{wallet[-4:]}: {lamports/LAMPORTS_PER_SOL:.6f} SOL")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
