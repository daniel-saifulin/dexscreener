-- Watcher: live signals from smart-money wallets + paper trades opened on those signals.

ALTER TABLE watched_wallets
    ADD COLUMN IF NOT EXISTS last_seen_signature TEXT,
    ADD COLUMN IF NOT EXISTS last_polled_at      TIMESTAMPTZ;

-- One row per detected swap event from a watched wallet.
CREATE TABLE IF NOT EXISTS wallet_signals (
    id                  BIGSERIAL PRIMARY KEY,
    wallet              TEXT NOT NULL,
    signature           TEXT NOT NULL,
    block_time          TIMESTAMPTZ NOT NULL,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chain               TEXT NOT NULL DEFAULT 'solana',
    action              TEXT NOT NULL,                -- 'buy' | 'sell'
    token_address       TEXT NOT NULL,
    token_symbol        TEXT,
    token_amount        NUMERIC,
    quote_mint          TEXT,
    quote_amount        NUMERIC,
    sol_amount          NUMERIC,
    -- Cross-references to screener data:
    candidate_passed    BOOLEAN,                     -- TRUE if token EVER passed our screener filters
    in_candidates       BOOLEAN,                     -- TRUE if DexScreener has ever surfaced this token
    raw                 JSONB,
    UNIQUE (wallet, signature, action, token_address)
);

CREATE INDEX IF NOT EXISTS idx_wallet_signals_block_time ON wallet_signals (block_time DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_signals_token      ON wallet_signals (chain, token_address);
CREATE INDEX IF NOT EXISTS idx_wallet_signals_wallet     ON wallet_signals (wallet, block_time DESC);

-- One row per paper-trade opened on a wallet buy signal.
CREATE TABLE IF NOT EXISTS wallet_paper_trades (
    id                    BIGSERIAL PRIMARY KEY,
    signal_id             BIGINT REFERENCES wallet_signals(id) UNIQUE,
    chain                 TEXT NOT NULL,
    token_address         TEXT NOT NULL,
    symbol                TEXT,
    triggered_by_wallet   TEXT NOT NULL,
    opened_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entry_price_usd       NUMERIC NOT NULL,
    stop_price_usd        NUMERIC NOT NULL,
    take_price_usd        NUMERIC NOT NULL,
    closed_at             TIMESTAMPTZ,
    exit_price_usd        NUMERIC,
    status                TEXT NOT NULL DEFAULT 'open',  -- open | closed_tp | closed_sl | closed_timeout | closed_no_price
    reason_out            TEXT,
    pnl_pct               NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_wpt_status     ON wallet_paper_trades (status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_wpt_token      ON wallet_paper_trades (chain, token_address);
