-- Gvy-solo экспериментальная стратегия.
-- Открываем paper-сделку на ЛЮБУЮ покупку Gvy, без cross-wallet conviction.
-- Цель — проверить переносится ли его 86% WR из cron-эры в новую систему.
-- Сделки помечены отдельным флагом чтобы не мешать когортам A/B/cron.

ALTER TABLE wallet_paper_trades
    ADD COLUMN IF NOT EXISTS from_solo_wallet     BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS solo_wallet_address  TEXT;

CREATE INDEX IF NOT EXISTS idx_wpt_solo
    ON wallet_paper_trades (from_solo_wallet, opened_at DESC)
    WHERE from_solo_wallet = TRUE;
