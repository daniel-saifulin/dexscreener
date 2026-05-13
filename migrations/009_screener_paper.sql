-- Гипотеза C: screener как независимая стратегия.
-- Цель: проверить даёт ли screener `passed_filters=TRUE` сам по себе прибыльную
-- стратегию, БЕЗ всякой связи с core-wallets. Отдельная когорта paper-сделок,
-- никакой дедупликации с wallet_paper_trades — каждая стратегия торгует своё.

CREATE TABLE IF NOT EXISTS screener_paper_trades (
    id              BIGSERIAL PRIMARY KEY,
    candidate_id    BIGINT REFERENCES candidates(id),
    chain           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    symbol          TEXT,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entry_price_usd NUMERIC NOT NULL,
    stop_price_usd  NUMERIC NOT NULL,
    take_price_usd  NUMERIC NOT NULL,
    closed_at       TIMESTAMPTZ,
    exit_price_usd  NUMERIC,
    status          TEXT NOT NULL DEFAULT 'open',  -- open / closed_tp / closed_sl / closed_max_hold / closed_no_price
    pnl_pct         NUMERIC,
    reason_out      TEXT
);

CREATE INDEX IF NOT EXISTS idx_screener_paper_status
    ON screener_paper_trades (status, opened_at DESC);

CREATE INDEX IF NOT EXISTS idx_screener_paper_token
    ON screener_paper_trades (chain, token_address);
