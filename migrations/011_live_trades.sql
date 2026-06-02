-- Live trading инфраструктура.
-- Полностью отдельная таблица от paper. Никакого пересечения данных.
-- Цель — отслеживать реальные swap'ы через Jupiter с полной аудит-цепочкой.

CREATE TABLE IF NOT EXISTS live_trades (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           BIGINT REFERENCES wallet_signals(id),
    source_wallet       TEXT NOT NULL,             -- какой smart-money триггерил
    chain               TEXT NOT NULL DEFAULT 'solana',
    token_address       TEXT NOT NULL,
    symbol              TEXT,

    -- Entry leg
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entry_sig           TEXT NOT NULL,             -- Solana transaction signature
    entry_price_usd     NUMERIC NOT NULL,
    entry_amount_usd    NUMERIC NOT NULL,          -- $ вложено
    entry_amount_tokens NUMERIC NOT NULL,          -- сколько токенов получили
    entry_slippage_pct  NUMERIC,                   -- реальный slippage в момент входа

    -- Risk limits зафиксированы при открытии
    stop_price_usd      NUMERIC NOT NULL,
    take_price_usd      NUMERIC NOT NULL,

    -- Exit leg (NULL пока open)
    closed_at           TIMESTAMPTZ,
    exit_sig            TEXT,
    exit_price_usd      NUMERIC,
    exit_amount_usd     NUMERIC,
    exit_slippage_pct   NUMERIC,

    -- Outcome
    status              TEXT NOT NULL DEFAULT 'open',
        -- open / closed_tp / closed_sl / closed_wallet_sold / closed_max_hold
        -- / closed_manual / closed_emergency
    pnl_pct             NUMERIC,
    pnl_usd             NUMERIC,
    reason_out          TEXT,

    -- Метаданные для аудита
    risk_guard_snapshot JSONB,                     -- состояние risk_guard на момент входа
    safety_check        JSONB                      -- результат safety_runtime проверок
);

CREATE INDEX IF NOT EXISTS idx_live_trades_status
    ON live_trades (status, opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_trades_token
    ON live_trades (chain, token_address);


-- Учёт капитала и daily limits.
CREATE TABLE IF NOT EXISTS live_capital_state (
    id                  SERIAL PRIMARY KEY,
    snapshot_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    wallet_balance_sol  NUMERIC,
    wallet_balance_usd  NUMERIC,
    peak_balance_usd    NUMERIC,
    daily_pnl_usd       NUMERIC,
    daily_anchor_date   DATE,
    is_halted           BOOLEAN NOT NULL DEFAULT FALSE,
    halt_reason         TEXT
);

-- Только одна "live" строка состояния, обновляем ON CONFLICT
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_capital_unique
    ON live_capital_state ((1));  -- хак для singleton: всегда (1)
