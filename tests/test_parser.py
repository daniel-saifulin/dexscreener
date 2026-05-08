"""Parser tests — pure, no network. Fixtures mimic Helius v0 enhanced format."""
from __future__ import annotations

from dexbot.parser import (
    LAMPORTS_PER_SOL,
    SOL_MINT,
    USDC_MINT,
    parse_swap,
    parse_swaps,
)


WALLET = "Wal1eTAddrEssAbCdEf1234567890XYZmoiETtest"
MEME_MINT = "Memec0inMintAaBbCc1234567890ABCDEFGHJKL"


def make_swap_tx(
    *, signature="sig1", timestamp=1_700_000_000, source="JUPITER",
    token_transfers=None, native_transfers=None, type_="SWAP",
) -> dict:
    return {
        "signature": signature,
        "timestamp": timestamp,
        "type": type_,
        "source": source,
        "tokenTransfers": token_transfers or [],
        "nativeTransfers": native_transfers or [],
    }


def test_buy_with_native_sol_legacy_form():
    tx = make_swap_tx(
        token_transfers=[
            {"mint": MEME_MINT, "tokenAmount": 1_000_000,
             "fromUserAccount": "router", "toUserAccount": WALLET},
        ],
        native_transfers=[
            {"amount": int(2.5 * LAMPORTS_PER_SOL),
             "fromUserAccount": WALLET, "toUserAccount": "router"},
        ],
    )
    ev = parse_swap(tx, WALLET)
    assert ev is not None
    assert ev.action == "buy"
    assert ev.token_mint == MEME_MINT
    assert ev.token_amount == 1_000_000
    assert ev.quote_mint == SOL_MINT
    assert ev.sol_amount == 2.5


def test_sell_into_usdc():
    tx = make_swap_tx(
        token_transfers=[
            {"mint": MEME_MINT, "tokenAmount": 500_000,
             "fromUserAccount": WALLET, "toUserAccount": "router"},
            {"mint": USDC_MINT, "tokenAmount": 120.5,
             "fromUserAccount": "router", "toUserAccount": WALLET},
        ],
    )
    ev = parse_swap(tx, WALLET)
    assert ev is not None
    assert ev.action == "sell"
    assert ev.token_mint == MEME_MINT
    assert ev.token_amount == 500_000
    assert ev.quote_mint == USDC_MINT
    assert ev.quote_amount == 120.5
    assert ev.sol_amount is None


def test_two_non_quote_tokens_skipped():
    """Multi-leg swaps with two non-quote tokens are not single-asset trades."""
    other = "OtHerMintZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
    tx = make_swap_tx(
        token_transfers=[
            {"mint": MEME_MINT, "tokenAmount": 1, "fromUserAccount": "r", "toUserAccount": WALLET},
            {"mint": other, "tokenAmount": 1, "fromUserAccount": "r", "toUserAccount": WALLET},
        ],
        native_transfers=[
            {"amount": LAMPORTS_PER_SOL, "fromUserAccount": WALLET, "toUserAccount": "r"},
        ],
    )
    assert parse_swap(tx, WALLET) is None


def test_unrelated_wallet_returns_none():
    tx = make_swap_tx(
        token_transfers=[
            {"mint": MEME_MINT, "tokenAmount": 1, "fromUserAccount": "x", "toUserAccount": "y"},
        ],
        native_transfers=[
            {"amount": LAMPORTS_PER_SOL, "fromUserAccount": "x", "toUserAccount": "y"},
        ],
    )
    assert parse_swap(tx, WALLET) is None


def test_dust_token_changes_filtered():
    """Tiny SPL rent-related residues shouldn't masquerade as a token leg."""
    tx = make_swap_tx(
        token_transfers=[
            {"mint": MEME_MINT, "tokenAmount": 1_000_000,
             "fromUserAccount": "r", "toUserAccount": WALLET},
            {"mint": "DustMintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "tokenAmount": 1e-12,
             "fromUserAccount": "r", "toUserAccount": WALLET},
        ],
        native_transfers=[
            {"amount": LAMPORTS_PER_SOL, "fromUserAccount": WALLET, "toUserAccount": "r"},
        ],
    )
    ev = parse_swap(tx, WALLET)
    assert ev is not None and ev.token_mint == MEME_MINT


def test_non_swap_tx_skipped():
    tx = make_swap_tx(type_="STAKE")
    assert parse_swap(tx, WALLET) is None


def test_parse_swaps_roundtrip():
    txs = [
        make_swap_tx(
            signature="s1",
            token_transfers=[
                {"mint": MEME_MINT, "tokenAmount": 100,
                 "fromUserAccount": "r", "toUserAccount": WALLET},
            ],
            native_transfers=[
                {"amount": LAMPORTS_PER_SOL, "fromUserAccount": WALLET, "toUserAccount": "r"},
            ],
        ),
        make_swap_tx(
            signature="s2",
            token_transfers=[
                {"mint": MEME_MINT, "tokenAmount": 100,
                 "fromUserAccount": WALLET, "toUserAccount": "r"},
                {"mint": USDC_MINT, "tokenAmount": 50,
                 "fromUserAccount": "r", "toUserAccount": WALLET},
            ],
        ),
        make_swap_tx(signature="s3", type_="STAKE"),  # ignored
    ]
    events = parse_swaps(txs, WALLET)
    assert [ev.action for ev in events] == ["buy", "sell"]
    assert [ev.signature for ev in events] == ["s1", "s2"]
