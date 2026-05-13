# Архитектура и фильтры — техническая документация

Полный обзор того как бот работает: какие компоненты, какие фильтры на каждом
этапе, какие данные где живут. Обновляется при каждом изменении логики.

## Схема потока данных

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              ИСТОЧНИКИ                                   │
└─────────────────────────────────────────────────────────────────────────┘

   DexScreener API                 Helius (push)                  Solana RPC
   (public, без ключа)             (webhook)                      (через Helius)
        │                               │                              │
        ▼                               ▼                              ▼
┌──────────────┐              ┌──────────────────┐          ┌──────────────────┐
│  SCREENER    │              │ WEBHOOK SERVER   │          │   DISCOVERY      │
│  (GH Actions │              │  (fly.io, FastAPI)│          │   (GH Actions    │
│   каждые 5м) │              │                  │          │    еженедельно)  │
└──────┬───────┘              └────────┬─────────┘          └────────┬─────────┘
       │                               │                              │
       ▼                               ▼                              ▼
   candidates                  wallet_signals                  watched_wallets
   таблица                     + pending_trades                таблица
                               + wallet_paper_trades
       │                               │
       ▼                               │
┌──────────────┐                       │
│  PROBES      │                       │
│  (GH Actions │                       │
│   каждые 30м)│                       │
└──────┬───────┘                       │
       │                               │
       ▼                               │
candidate_probes                       │
                                       │
       ┌───────────────────────────────┘
       │
       ▼
┌──────────────┐
│   MONITOR    │  (внутри Watcher cron, GH Actions, каждые 10 мин)
│              │  Закрывает открытые paper-сделки по TP/SL/max_hold
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   ANALYSIS   │  (GH Actions, каждые 4ч)
│              │  Markdown-отчёт в GITHUB_STEP_SUMMARY
└──────────────┘
```

## Детальная схема — путь от события до paper-сделки

```
Helius блокчейн-индексер видит свап одного из 6 core-кошельков
                              │
                              ▼ POST https://dexbot-webhook.fly.dev/webhook/helius
                              │ (latency 1-3 секунды)
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  fly.io webhook_server.py: process_one_tx(tx, core_set)                  │
│                                                                          │
│  1. parse_swap() → SwapEvent {wallet, action, token, sol_amount}        │
│                                                                          │
│  2. ┌── action == "sell" ──────────────────────────────────────────┐    │
│     │ Закрываем все открытые paper_trades где                      │    │
│     │ triggered_by_wallet = этот кошелёк AND token = этот токен    │    │
│     │ статус = 'closed_wallet_sold', exit_price = текущий DexScrn  │    │
│     └──────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  3. ┌── action == "buy" ───────────────────────────────────────────┐    │
│     │ INSERT INTO wallet_signals (всегда, для аналитики)            │    │
│     │                                                                │   │
│     │ Если cross-wallet conviction ≥2 core в окне 30 мин            │    │
│     │ И нет открытой сделки на этот токен в последние 24ч:          │    │
│     │   INSERT INTO pending_trades с open_after_at = NOW() + 5 мин  │    │
│     └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (через 5 минут)
┌─────────────────────────────────────────────────────────────────────────┐
│  pending_worker (asyncio, каждые 30 сек)                                │
│                                                                          │
│  SELECT FROM pending_trades WHERE open_after_at <= NOW()                │
│  и processed_at IS NULL                                                  │
│                                                                          │
│  Для каждой:                                                             │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │ a) Re-check: всё ещё ≥2 core купили этот токен за последние 30м?│   │
│  │    Нет → decision='dropped_no_conviction'                       │    │
│  │                                                                   │    │
│  │ b) Re-check dedup: нет открытой сделки на этот токен (24ч)?      │    │
│  │    Уже есть → decision='dropped_dedup'                          │    │
│  │                                                                   │    │
│  │ c) Получаем СВЕЖУЮ цену из DexScreener                          │    │
│  │    Нет цены → decision='dropped_no_price'                       │    │
│  │                                                                   │    │
│  │ d) INSERT INTO wallet_paper_trades                              │    │
│  │    from_core_conviction=TRUE, from_webhook=TRUE,                │    │
│  │    webhook_delay_min=5, status='open'                           │    │
│  │    decision='opened'                                            │    │
│  └────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (каждые 10 минут — Watcher cron на GH Actions)
┌─────────────────────────────────────────────────────────────────────────┐
│  Monitor: для каждой open paper-сделки                                  │
│                                                                          │
│  Получает текущую цену (батч до 30 токенов за раз)                      │
│  Принимает решение:                                                      │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │ price ≤ stop (−12%)  → status='closed_sl'                       │    │
│  │ price ≥ take (+18%)  → status='closed_tp'                       │    │
│  │ age ≥ 168 часов      → status='closed_max_hold' (sanity-лимит) │    │
│  └────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

## Все фильтры по этапам

### 1. Discovery — кого вообще watch'им

**Где**: `dexbot/discovery.py`, `dexbot/harvest.py`
**Когда**: Воскресенье 03:00 UTC + ручной запуск
**Что фильтрует**: кошельки которые попадают в `watched_wallets`

| Источник | Критерий |
|---|---|
| harvest_pool (из `candidates`) | Кошелёк фигурирует в покупке ≥2 разных токенов из недавних candidates |
| manual add | Через CLI `discovery add ADDR` |

**Scoring threshold** для опроса Watcher'ом: `score >= 30`.

### 2. Screener — какие токены берём в кандидаты

**Где**: `dexbot/screener.py`
**Когда**: GH Actions cron, каждые 5 мин
**Что фильтрует**: DexScreener-листинг → `candidates` таблица с `passed_filters` boolean

| Фильтр | Порог | Параметр в config |
|---|---|---|
| Цепь | solana / base / ethereum | `chains` |
| Ликвидность пула | ≥ $30,000 | `min_liquidity_usd` |
| Ликвидность пула | ≤ $2,000,000 | `max_liquidity_usd` |
| Возраст пары | ≥ 30 мин | `min_age_minutes` |
| Возраст пары | ≤ 24 часа | `max_age_hours` |
| Объём за 1ч | ≥ $5,000 | `min_volume_h1_usd` |
| Изменение цены 1ч | ≥ −10% | `min_price_change_h1` |
| Изменение цены 1ч | ≤ +200% | `max_price_change_h1` |
| Доля покупок за 1ч | ≥ 55% | `min_buy_ratio_h1` |

**Safety проверки** (если предыдущие фильтры пройдены):

| Источник | Критерий |
|---|---|
| GoPlus Security (EVM) | sell_tax ≤ 5%, buy_tax ≤ 5%, top10 ≤ 60%, не honeypot, нет опасных флагов (proxy, blacklisted, mintable) |
| RugCheck (Solana) | Стандартные risk-flags из их анализа |

### 3. Probes — shadow paper-trade для замера качества screener'а

**Где**: `dexbot/probes.py`
**Когда**: GH Actions cron, каждые 30 мин
**Что фильтрует**: токены задетектированные в последние 25 часов, у которых ещё не было probe в последние 25 мин

Не открывает реальных paper-сделок — только записывает `pct_change` от первой цены детекта. Используется для **отдельного аналитического измерения**: насколько фильтры screener'а отделяют сигнал от шума.

### 4. Watcher (cron) — логирование сигналов от ВСЕХ active кошельков

**Где**: `dexbot/watcher.py`
**Когда**: GH Actions cron, каждые 10 мин
**Режим**: `WEBHOOK_HANDLES_CORE=true` — **только логирование, без открытия core-сделок**. Открытие — за webhook'ом.

| Фильтр | Назначение |
|---|---|
| `wallet.is_active = TRUE` AND `score >= 30` | Кого опрашивать |
| Возраст события ≤ 6 часов | Не действуем на стары́е сигналы |
| ≤ 10 сделок за один полл-цикл на кошелёк | Защита от каскада на первом запуске |
| `action == "buy"` | Сейчас открываем только лонги |

### 5. Webhook server — push-обработка core-кошельков

**Где**: `dexbot/webhook_server.py` на fly.io
**Когда**: реактивно от Helius (latency 1-3 сек)
**Что фильтрует**:

| Фильтр | Назначение |
|---|---|
| Возраст события ≤ 1 час | Игнор старых событий (например, retry после downtime) |
| `wallet` ∈ core_set | Обрабатываем только сигналы от 6 ядерных |
| `action == "sell"` → follower-exit | Закрываем открытые позиции этого кошелька на этом токене |
| `action == "buy"` → переход к 6 (core conviction) | Buy идёт дальше |

### 6. Core conviction — открытие paper-сделки

**Где**: webhook_server.py, `process_one_tx` + `pending_worker`
**Когда**: всегда, но открытие отложено на 5 минут

**Receive-time checks** (мгновенно):

| Фильтр | Назначение |
|---|---|
| Есть ≥ 2 разных core-кошелька с buy на этот токен в последние 30 мин | Базовая cross-wallet конвикция |
| Нет открытой/закрытой paper-сделки на этот токен в последние 24ч | Дедуп |

Если оба пройдены → INSERT INTO `pending_trades` с `open_after_at = NOW() + 5 минут`.

**Decision-time checks** (через 5 мин, в pending_worker):

| Фильтр | Назначение |
|---|---|
| Conviction ≥2 core всё ещё актуальна | Защита от "сразу-передумали" |
| Дедуп всё ещё актуален | Не открыть дубль если кто-то успел открыть |
| DexScreener вернул цену | Без цены сделку не открываем |

Если всё ок → INSERT INTO `wallet_paper_trades`:
```
status='open', from_core_conviction=TRUE, from_webhook=TRUE,
webhook_delay_min=5, entry_price=current_dexscreener_price,
stop=entry*0.88, take=entry*1.18, triggered_by_wallet=second_buyer
```

### 7. Monitor — закрытие открытых сделок

**Где**: `dexbot/watcher.py`, `monitor_open_trades()`
**Когда**: каждый Watcher cron tick (каждые 10 мин)

Для каждой open paper-сделки получает текущую цену (батчем) и принимает решение:

| Условие | Статус закрытия |
|---|---|
| Цена ≤ stop (entry × 0.88) | `closed_sl` |
| Цена ≥ take (entry × 1.18) | `closed_tp` |
| Возраст ≥ 168 часов (7 дней) | `closed_max_hold` |

И отдельно через webhook (push, мгновенно):

| Условие | Статус закрытия |
|---|---|
| `triggered_by_wallet` продал токен | `closed_wallet_sold` |

## Все возможные статусы paper-сделки

| Статус | Кто записывает | Когда |
|---|---|---|
| `open` | webhook / cron | При INSERT |
| `closed_tp` | monitor (cron) | Цена ≥ +18% |
| `closed_sl` | monitor (cron) | Цена ≤ −12% |
| `closed_wallet_sold` | webhook (push) | Core-wallet продал этот токен |
| `closed_max_hold` | monitor (cron) | Сделка 168ч в `open`, никто не сработал |
| `closed_timeout` | _**(deprecated)**_ | Старый 24-часовой лимит до 2026-05-13 |
| `closed_no_price` | _резерв_ | DexScreener не вернул цену для закрытия |
| `closed_manual` | _резерв_ | Ручное вмешательство оператора |

## Текущая конфигурация (2026-05-13)

| Параметр | Значение | Файл |
|---|---|---|
| TP | +18.0% | `dexbot/watcher.py:TP_PCT` |
| SL | −12.0% | `dexbot/watcher.py:SL_PCT` |
| Hard timeout | 168 часов (7 дней) | `dexbot/watcher.py:TIMEOUT_HOURS` |
| Min core buyers | 2 | `dexbot/watcher.py:MIN_CORE_BUYERS` |
| Core conviction window | 30 мин | `dexbot/watcher.py:CORE_CONVICTION_WINDOW_MIN` |
| Dedup window | 24 часа | `dexbot/watcher.py:DEDUP_RECENT_TRADE_HOURS` |
| Watcher poll | 10 мин | `.github/workflows/watcher.yml` |
| Webhook delay | 5 мин | env `WEBHOOK_DELAY_MIN` |
| Pending worker | 30 сек | `dexbot/webhook_server.py:PENDING_WORKER_INTERVAL_SEC` |
| Min wallet score | 30 | `dexbot/watcher.py:MIN_WALLET_SCORE` |
| Min liquidity (screener) | $30,000 | `dexbot/config.py` |
| Max liquidity (screener) | $2,000,000 | `dexbot/config.py` |

## Где живут данные

```
Neon Postgres (free tier 0.5 GB)
├── candidates              — Что DexScreener показал
├── candidate_probes        — Серия цен после первого детекта
├── safety_cache            — Кэш GoPlus / RugCheck ответов
├── watched_wallets         — Pool потенциальных смарт-мани
│   └── is_core flag        — Подмножество "ядра" (сейчас 6)
├── wallet_signals          — Каждое buy/sell-событие наших кошельков
├── wallet_paper_trades     — Бумажные позиции
└── pending_trades          — Очередь отложенных открытий (delayed-webhook)
```

## Эксперимент latency (активен с 2026-05-12)

Три когорты для сравнения, все на одинаковой стратегии cross-wallet conviction:

| Когорта | Где открывается | Latency от on-chain до paper-сделки |
|---|---|---|
| `cron (10min)` | `from_webhook=FALSE` | 5-15 минут (последнее открытие до 2026-05-11 14:50 UTC) |
| `instant-webhook (10s)` | `from_webhook=TRUE`, `webhook_delay_min IS NULL` | 5-15 секунд (между 2026-05-11 и 2026-05-12 12:25 UTC) |
| `delayed-webhook (5min)` | `webhook_delay_min = 5` | 5-7 минут (с 2026-05-12 12:25 UTC) |

Цель: понять помогает ли низкая latency или наоборот — задержка работает как фильтр ложных сигналов.

## Где смотреть метрики

| Что | Где |
|---|---|
| Отчёты каждые 4ч | <https://github.com/daniel-saifulin/dexscreener/actions/workflows/analysis.yml> |
| Локальный отчёт | `cd /Users/family/crypto && source .venv/bin/activate && python -m dexbot.analysis` |
| Статус fly.io webhook'а | `curl https://dexbot-webhook.fly.dev/health` |
| Состояние pending очереди | `curl https://dexbot-webhook.fly.dev/pending` |
| Текущее ядро | `curl https://dexbot-webhook.fly.dev/core` или `python -m dexbot.discovery list-core` |
| Расшифровка таблиц в отчёте | `reports.md` |

## Команды для управления

```bash
cd /Users/family/crypto && source .venv/bin/activate

# Анализ и состояние
python -m dexbot.analysis
python -m dexbot.discovery list-core
python -m dexbot.discovery list --limit 30

# Управление core-набором
python -m dexbot.discovery promote ADDR
python -m dexbot.discovery demote ADDR

# После любого promote/demote — синхронизировать Helius
python -m dexbot.setup_helius_webhook update <WEBHOOK_ID>

# Принудительный запуск воркфлоу
gh workflow run watcher.yml --repo daniel-saifulin/dexscreener
gh workflow run discovery.yml --repo daniel-saifulin/dexscreener
gh workflow run analysis.yml --repo daniel-saifulin/dexscreener

# Логи fly.io
fly logs --app dexbot-webhook
fly status --app dexbot-webhook
```
