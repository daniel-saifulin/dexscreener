-- Shadow paper-trader: track price evolution of every detected token
-- for 24h after first detection, regardless of pass/fail.
CREATE TABLE IF NOT EXISTS candidate_probes (
    id              BIGSERIAL PRIMARY KEY,
    chain           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    probed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    age_minutes     INT NOT NULL,                -- minutes since first detection of this token
    price_usd       NUMERIC NOT NULL,
    pct_change      NUMERIC NOT NULL,            -- vs first-seen price_usd, in percent
    passed_filters  BOOLEAN NOT NULL DEFAULT FALSE   -- did any detect-time row pass our hard filters?
);

CREATE INDEX IF NOT EXISTS idx_candidate_probes_token_time
    ON candidate_probes (chain, token_address, probed_at DESC);

CREATE INDEX IF NOT EXISTS idx_candidate_probes_age
    ON candidate_probes (age_minutes);

CREATE INDEX IF NOT EXISTS idx_candidate_probes_passed_age
    ON candidate_probes (passed_filters, age_minutes);
