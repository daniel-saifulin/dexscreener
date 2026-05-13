-- Гипотеза B: pool_age annotation для core-buy сигналов.
-- 94% core-buy сигналов идут на токены вне screener candidates — нам нужны
-- on-chain метрики чтобы понять в какие пулы заходят core-кошельки.

ALTER TABLE wallet_signals
    ADD COLUMN IF NOT EXISTS pool_age_at_signal_min INT,
    ADD COLUMN IF NOT EXISTS pool_address TEXT;

CREATE INDEX IF NOT EXISTS idx_wallet_signals_pool
    ON wallet_signals (pool_address) WHERE pool_address IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_wallet_signals_pool_age
    ON wallet_signals (pool_age_at_signal_min) WHERE pool_age_at_signal_min IS NOT NULL;

-- Персистентный кэш: один pool/mint опрашиваем один раз.
-- ОБЯЗАТЕЛЬНО — иначе burn Helius credits на повторных вызовах.
CREATE TABLE IF NOT EXISTS pool_metadata (
    address        TEXT PRIMARY KEY,            -- token mint или pool address
    chain          TEXT NOT NULL DEFAULT 'solana',
    creation_ts    TIMESTAMPTZ,
    source         TEXT,                        -- 'dexscreener' | 'helius'
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error          TEXT                         -- если последняя попытка упала
);

CREATE INDEX IF NOT EXISTS idx_pool_metadata_creation
    ON pool_metadata (creation_ts) WHERE creation_ts IS NOT NULL;
