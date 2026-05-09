-- Стратегия "Уровень 2": бот опрашивает все активные кошельки и логирует
-- сигналы, но открывает paper-сделки только когда ≥2 разных core-кошельков
-- купили один токен в окне 30 минут.

ALTER TABLE watched_wallets
    ADD COLUMN IF NOT EXISTS is_core      BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS core_set_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_watched_wallets_core
    ON watched_wallets (is_core, is_active);

-- На paper-сделке отмечаем, открыта ли она по новой логике (core conviction)
-- или это легаси трейд от полного watch-листа. Полезно для разделения
-- статистики до/после переключения.
ALTER TABLE wallet_paper_trades
    ADD COLUMN IF NOT EXISTS from_core_conviction BOOLEAN NOT NULL DEFAULT FALSE;

-- Стартовый core-набор: 3 кошелька с медианой PnL ≥+20% и WR ≥60% на ≥34 закрытых.
UPDATE watched_wallets
SET is_core = TRUE, core_set_at = NOW()
WHERE address IN (
    'BioBT877DVAo7DD6MVaYkMyGZ7qUQ3Lthn5cqyGG6ons',
    'D9LcgmcrfNUg8nS9YHqe2H6eSyK9GBE5R5BMkwNarCe3',
    'SHARKRdGLNYRZrhotqvZi3XAtT62CRGCFxmg5LJgSHC'
);
