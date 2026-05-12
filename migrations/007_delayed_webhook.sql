-- Delayed-webhook эксперимент.
-- Гипотеза: cron работает лучше webhook из-за того что "поздняя" детекция
-- фильтрует ложные конвикции (когда вторая покупка — мелкая разведка,
-- а не серьёзная позиция). Эмулируем это поведение на webhook'е:
-- получаем событие → откладываем решение на N минут → проверяем что
-- conviction всё ещё актуальна → открываем.

CREATE TABLE IF NOT EXISTS pending_trades (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           BIGINT REFERENCES wallet_signals(id) UNIQUE,
    chain               TEXT NOT NULL DEFAULT 'solana',
    token_address       TEXT NOT NULL,
    triggered_by_wallet TEXT NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    open_after_at       TIMESTAMPTZ NOT NULL,
    processed_at        TIMESTAMPTZ,
    decision            TEXT,        -- opened | dropped_no_conviction | dropped_dedup | dropped_no_price | dropped_error
    trade_id            BIGINT REFERENCES wallet_paper_trades(id),
    error               TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_pending
    ON pending_trades (open_after_at) WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_pending_token
    ON pending_trades (chain, token_address);

-- Помечаем сделки открытые через delayed-webhook (вместо мгновенного открытия)
ALTER TABLE wallet_paper_trades
    ADD COLUMN IF NOT EXISTS webhook_delay_min INT;
