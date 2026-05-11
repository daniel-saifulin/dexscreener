-- Помечает paper-сделки открытые через Helius webhook (низкая latency)
-- в отличие от cron-Watcher (10-минутный polling). Используется для
-- сравнения двух выборок: latency-experiment.

ALTER TABLE wallet_paper_trades
    ADD COLUMN IF NOT EXISTS from_webhook BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_wpt_webhook
    ON wallet_paper_trades (from_webhook, opened_at DESC);
