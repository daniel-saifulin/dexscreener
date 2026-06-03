-- Дополнительные поля для точного отслеживания живых сделок.
-- USD-стоимость драфтуется от SOL цены — нужны SOL-lamports как ground-truth.

ALTER TABLE live_trades
    ADD COLUMN IF NOT EXISTS entry_sol_lamports     BIGINT,
    ADD COLUMN IF NOT EXISTS exit_sol_lamports      BIGINT,
    ADD COLUMN IF NOT EXISTS sol_price_usd_at_entry NUMERIC,
    ADD COLUMN IF NOT EXISTS sol_price_usd_at_exit  NUMERIC;
