# Текущее состояние проекта

Обновляется при каждом крупном изменении стратегии.

## На какой стадии

**Уровень 2: paper-trading на cross-wallet conviction. Latency-эксперимент активен (с 2026-05-11).** Реальная торговля ещё не запущена, депозит не тронут.

## Стратегия в одном абзаце

Бот опрашивает 85+ Solana-кошельков (полный список в БД). 7 из них помечены как «core» (золотая стратегия). Когда ≥2 разных core-кошельков покупают один и тот же токен в окне 30 минут — открывается бумажная сделка с целью +18% и стопом −12%, держим до 24 часов. Сигналы от не-core кошельков логируются для скоринга, но не торгуются.

Открытием core-сделок занимается **fly.io webhook-сервер** на push-сигналах Helius (latency ~5-15 сек). GitHub Actions cron-Watcher остаётся как fallback и для логирования сигналов от не-core кошельков.

## Текущее ядро (6 кошельков, BioBT877 демотирован 2026-05-12)

| Кошелёк | Закрыто | WR (без таймаутов) | Медиана PnL | Замечание |
|---|---:|---:|---:|---|
| `GvyLS9WFxUBzoiVPKTJAR2bGLocnoEVWRYh4D8i5z7m1` | 185 | **87%** | +28.1% | звезда |
| `BSfQT2AmdxQfpsGQANrcYEUcwta5PWuTfBkHxqsZ3Gz8` | 82 | **79%** | +18.1% | сильный |
| `D9LcgmcrfNUg8nS9YHqe2H6eSyK9GBE5R5BMkwNarCe3` | 267 | 52% | +5.6% | стабильный, большая выборка |
| `8L2y55D11k63CAftvW7uMM2mBhtMxLoLnivG9uY2bt8j` | 454 | 55% | +2.7% | лотерейный, mean +62% |
| `SHARKRdGLNYRZrhotqvZi3XAtT62CRGCFxmg5LJgSHC` | 180 | 50% | +2.4% | лотерейный, mean +163% |
| `ESuvjvsQtjuxC4XGsDeMhx8Wp5yjQcCFGncGhupcJbg8` | 268 | 57% | +1.3% | 67% таймаутов, медленные сигналы |

**Демотирован 2026-05-12**: `BioBT877` — на 79 закрытых WR упал до 43%, медиана −12.5%. Демотация по критерию.

## Критерии управления core-набором

- **Повышение в core**: ≥100 закрытых, WR ≥55% (без таймаутов), медиана ≥0%
- **Демотация / деактивация**: ≥50 закрытых **и** (WR <45% или медиана <0%)
- WR всегда считаем как `wins / (wins + losses)` — таймауты исключаем

## Что осталось до реальной торговли

| Блок | Статус |
|---|---|
| ≥30 закрытых core-сделок | 15 / 30 |
| WR ≥55% на этих 30 | n/a (рано) |
| Медиана PnL ≥+15% | n/a (рано) |
| 7 дней наблюдения core-стратегии | ~3 / 7 |
| **Helius webhooks + fly.io (latency <15 сек)** | ✅ запущено 2026-05-11 |
| Модуль `live.py` (Jupiter swap) | не написан |
| Модуль `safety_runtime.py` (pre-swap проверка) | не написан |
| Модуль `risk_guard.py` (kill-switch, лимиты) | не написан |
| Тест на Devnet | не сделан |

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

# После promote/demote — синхронизировать Helius webhook с новым ядром
python -m dexbot.setup_helius_webhook update c3249c3b-ef1e-4f36-82b4-4e3018814fc0

# Принудительный запуск чего-либо
gh workflow run watcher.yml --repo daniel-saifulin/dexscreener
gh workflow run discovery.yml --repo daniel-saifulin/dexscreener
gh workflow run analysis.yml --repo daniel-saifulin/dexscreener

# Логи fly.io webhook'а
fly logs --app dexbot-webhook
fly status --app dexbot-webhook
curl https://dexbot-webhook.fly.dev/health
curl https://dexbot-webhook.fly.dev/core
```

## Файлы проекта

| Что | Где |
|---|---|
| Расшифровка таблиц отчёта | `reports.md` |
| Этот файл (состояние) | `PROJECT_STATE.md` |
| Код | `dexbot/*.py` |
| Webhook-сервер (fly.io) | `dexbot/webhook_server.py` + `Dockerfile` + `fly.toml` |
| Миграции БД | `migrations/*.sql` |
| Воркфлоу | `.github/workflows/*.yml` |
| Тесты | `tests/test_*.py` |
| Локальные креды (gitignored) | `.env` |

## Ключевые исторические решения

- **2026-05-08**: переключились с trend/mean-reversion стратегии на BTC/ETH (была убыточна на бэктесте −10%) на DexScreener-сканер мемкоинов
- **2026-05-09**: добавили Watcher (копи-трейд) и Probes (shadow paper-trade)
- **2026-05-09 утром**: преждевременно деактивировал `BSfQT2Am` на 8 закрытых при WR 0% — **урок: ниже 20 закрытых выводов не делаем**. Сохранён в персистентную память.
- **2026-05-09 вечером**: запустил Уровень 2 с **3 ядерными** кошельками (BioBT877, D9LcgmcrfNU, SHARKRdG). Cron-Watcher открывает core-сделки только на cross-wallet conviction ≥2.
- **2026-05-10 утром**: реактивировал `BSfQT2Am` (его открытые сделки докрутились до +172% медианы на 39 закрытых) и расширил ядро до **6**, добавив `GvyLS9WF` и `ESuvjvsQ`.
- **2026-05-10 вечером**: обнаружил **баг в метрике WR** в разделе Promotion candidates — таймауты считались как «не победы», что прятало валидных кандидатов. Починил. По правильной метрике повысил **8L2y55D1** → ядро стало **7 кошельков**. Ужесточил порог: ≥100 закрытых (было ≥20). Урок сохранён в память.
- **2026-05-11 утром**: тот же баг WR-метрики оказался и в разделе Health of current core wallets — починил, добавил колонку timeout%. Обнаружил что у ESuvjvsQ 68% сделок завершаются таймаутом (медленные сигналы) — оставил на испытательном сроке.
- **2026-05-11 днём**: **развернул webhook-инфру на fly.io**. Helius webhook id `c3249c3b-ef1e-4f36-82b4-4e3018814fc0` подписан на 7 ядерных кошельков. Cron-Watcher переключён в режим `WEBHOOK_HANDLES_CORE=true` — только логирует, не открывает core-сделок (это делает webhook). Latency: 5-15 минут → 5-15 секунд (×50 быстрее). Эксперимент: через 48 часов сравнить когорты `from_webhook=TRUE` vs `from_webhook=FALSE` — если webhook-когорта заметно прибыльнее, latency была главным узким местом.
- **2026-05-11 днём**: чек-лист до live-торговли не закрыт: 15/30 закрытых core-сделок, ~3/7 дней наблюдения, нет модулей `live.py` / `safety_runtime.py` / `risk_guard.py`. Live не подключается до сходимости статистики на низкой latency.
- **2026-05-12 утром**: первая полная итерация latency-эксперимента. **Webhook (10 сек) проиграл cron'у (10 мин) по всем метрикам**: WR 21% vs 59%, mean −2.7% vs +9.6%. Гипотеза: cron-задержка работает как фильтр ложных конвикций. Параллельно: core-стратегия в целом на +1.8% mean (слабо), BioBT877 обвалился до медианы −12.5%.
- **2026-05-12 днём**: **запущен delayed-webhook эксперимент**. Логика: webhook принимает событие мгновенно, инсертит сигнал, ставит в очередь `pending_trades` с задержкой 5 мин. Asyncio worker через 5 мин перепроверяет conviction → открывает или дропает. Эмулируем cron-фильтрацию без потери преимущества по latency. **BioBT877 демотирован из ядра**. Ядро сократилось до 6. Helius webhook пересинхронизирован. Deploy на fly.io прошёл со 2-й попытки (`--strategy immediate`).
