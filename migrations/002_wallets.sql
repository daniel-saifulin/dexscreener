-- Wallets we currently track.
CREATE TABLE IF NOT EXISTS watched_wallets (
    address             TEXT PRIMARY KEY,
    chain               TEXT NOT NULL DEFAULT 'solana',
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source              TEXT,                    -- 'manual' | 'co_buyer' | 'gmgn' | etc.
    last_scored_at      TIMESTAMPTZ,
    score               NUMERIC,                 -- composite, higher = better
    trades_30d          INT,
    distinct_tokens_30d INT,
    buy_volume_30d_sol  NUMERIC,
    sell_volume_30d_sol NUMERIC,
    realized_pnl_30d_sol NUMERIC,
    last_active_at      TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_watched_wallets_active ON watched_wallets (is_active, score DESC NULLS LAST);

-- Per-transaction parsed activity. Used both for scoring and (later) live signals.
CREATE TABLE IF NOT EXISTS wallet_activity (
    id            BIGSERIAL PRIMARY KEY,
    wallet        TEXT NOT NULL,
    signature     TEXT NOT NULL,
    block_time    TIMESTAMPTZ NOT NULL,
    chain         TEXT NOT NULL DEFAULT 'solana',
    action        TEXT NOT NULL,              -- 'buy' | 'sell' | 'other'
    token_address TEXT,
    token_symbol  TEXT,
    token_amount  NUMERIC,
    sol_amount    NUMERIC,
    usd_amount    NUMERIC,
    source        TEXT,                       -- DEX router source, if known (Jupiter, Raydium, etc.)
    raw           JSONB,
    UNIQUE (wallet, signature, action, token_address)
);

CREATE INDEX IF NOT EXISTS idx_wallet_activity_wallet_time ON wallet_activity (wallet, block_time DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_activity_token       ON wallet_activity (token_address, block_time DESC);
