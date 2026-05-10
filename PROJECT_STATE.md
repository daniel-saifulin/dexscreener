# Текущее состояние проекта

Обновляется при каждом крупном изменении стратегии.

## На какой стадии

**Уровень 2: paper-trading на cross-wallet conviction.** Реальная торговля ещё не запущена, депозит не тронут.

## Стратегия в одном абзаце

Бот опрашивает 34+ Solana-кошельков (полный список в БД). 6 из них помечены как «core» (золотая стратегия). Когда ≥2 разных core-кошельков покупают один и тот же токен в окне 30 минут — открывается бумажная сделка с целью +18% и стопом −12%, держим до 24 часов. Сигналы от не-core кошельков логируются для скоринга, но не торгуются.

## Текущее ядро (6 кошельков)

| Кошелёк | Закрыто | WR | Медиана PnL |
|---|---:|---:|---:|
| `D9LcgmcrfNUg8nS9YHqe2H6eSyK9GBE5R5BMkwNarCe3` | 192 | 55% | +19.1% |
| `BioBT877DVAo7DD6MVaYkMyGZ7qUQ3Lthn5cqyGG6ons` | 47 | 57% | +18.7% |
| `SHARKRdGLNYRZrhotqvZi3XAtT62CRGCFxmg5LJgSHC` | 56 | 54% | +18.5% |
| `BSfQT2AmdxQfpsGQANrcYEUcwta5PWuTfBkHxqsZ3Gz8` | 39 | 67% | +172.3% |
| `GvyLS9WFxUBzoiVPKTJAR2bGLocnoEVWRYh4D8i5z7m1` | 40 | 88% | +175.5% |
| `ESuvjvsQtjuxC4-...` (полный адрес в БД) | 47 | 60% | +19.0% |

## Что осталось до реальной торговли

| Блок | Статус |
|---|---|
| ≥30 закрытых core-сделок | 0 / 30 |
| WR ≥55% на этих 30 | n/a |
| Медиана PnL ≥+15% | n/a |
| 7 дней наблюдения | 0 / 7 |
| Модуль `live.py` (Jupiter swap) | не написан |
| Модуль `safety_runtime.py` (pre-swap проверка) | не написан |
| Модуль `risk_guard.py` (kill-switch, лимиты) | не написан |
| Helius webhooks + fly.io (latency <15 сек) | не настроено |
| Тест на Devnet | не сделано |

## Защиты которые должны быть в live-коде

- Минимум ликвидности пула $50k для входа
- Размер позиции $20 максимум
- Максимум 3 одновременные позиции
- Daily kill-switch при −20% от старта дня
- Jupiter `slippageBps=300` (3% максимум на свапе)
- Jito bundles (защита от MEV-сэндвича)
- GoPlus + RugCheck в момент свапа (не до)
- Приватный ключ ТОЛЬКО в fly.io secrets, никогда в коде/git
- Кошелёк фондируется ровно $200, не больше

## Команды

```bash
cd /Users/family/crypto && source .venv/bin/activate

# Посмотреть отчёт
python -m dexbot.analysis

# Управление core-набором
python -m dexbot.discovery list-core
python -m dexbot.discovery promote ADDRESS
python -m dexbot.discovery demote ADDRESS

# Принудительный запуск чего-либо
gh workflow run watcher.yml --repo daniel-saifulin/dexscreener
gh workflow run discovery.yml --repo daniel-saifulin/dexscreener
gh workflow run analysis.yml --repo daniel-saifulin/dexscreener
```

## Файлы проекта

| Что | Где |
|---|---|
| Расшифровка таблиц отчёта | `reports.md` |
| Этот файл (состояние) | `PROJECT_STATE.md` |
| Код | `dexbot/*.py` |
| Миграции БД | `migrations/*.sql` |
| Воркфлоу | `.github/workflows/*.yml` |
| Тесты | `tests/test_*.py` |
| Локальные креды (gitignored) | `.env` |

## Ключевые исторические решения

- **2026-05-08**: переключились с trend/mean-reversion стратегии на BTC/ETH (была убыточна) на DexScreener-сканер мемкоинов
- **2026-05-09**: добавили Watcher (копи-трейд) и Probes (shadow paper-trade)
- **2026-05-09 утром**: преждевременно деактивировал `BSfQT2Am` на 8 закрытых — урок про размер выборки
- **2026-05-09 вечером**: запустил Уровень 2 с 3 ядерными кошельками
- **2026-05-10 утром**: расширил ядро до 6 кошельков, переоткрыл `BSfQT2Am`
