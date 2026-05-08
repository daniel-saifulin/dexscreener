-- Every detected token-pair, regardless of pass/fail. Append-only.
CREATE TABLE IF NOT EXISTS candidates (
    id              BIGSERIAL PRIMARY KEY,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chain           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    pair_address    TEXT NOT NULL,
    symbol          TEXT,
    name            TEXT,
    price_usd       NUMERIC,
    liquidity_usd   NUMERIC,
    volume_h1_usd   NUMERIC,
    price_change_h1 NUMERIC,
    pair_age_minutes INT,
    buys_h1         INT,
    sells_h1        INT,
    passed_filters  BOOLEAN NOT NULL DEFAULT FALSE,
    filter_reasons  JSONB,
    safety_flags    JSONB,
    raw             JSONB
);

CREATE INDEX IF NOT EXISTS idx_candidates_token_chain  ON candidates (chain, token_address);
CREATE INDEX IF NOT EXISTS idx_candidates_detected_at  ON candidates (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_passed       ON candidates (passed_filters, detected_at DESC);

-- Cache GoPlus / RugCheck responses to stay under their rate limits.
CREATE TABLE IF NOT EXISTS safety_cache (
    chain         TEXT NOT NULL,
    token_address TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    flags         JSONB,
    PRIMARY KEY (chain, token_address)
);

-- Open + closed paper trades. Unused in screener, used by future monitor.
CREATE TABLE IF NOT EXISTS paper_positions (
    id            BIGSERIAL PRIMARY KEY,
    candidate_id  BIGINT REFERENCES candidates(id),
    chain         TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol        TEXT,
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at     TIMESTAMPTZ,
    entry_price   NUMERIC NOT NULL,
    exit_price    NUMERIC,
    stop_price    NUMERIC NOT NULL,
    take_price    NUMERIC NOT NULL,
    size_usd      NUMERIC,
    status        TEXT NOT NULL DEFAULT 'open',
    pnl_pct       NUMERIC,
    reason_in     TEXT,
    reason_out    TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions (status);
CREATE INDEX IF NOT EXISTS idx_paper_positions_token  ON paper_positions (chain, token_address);
