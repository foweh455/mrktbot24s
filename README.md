# MRKT NFT Gift Sniper Bot

Асинхронный бот-снайпер для [MRKT](https://t.me/mrkt) — площадки торговли Telegram NFT-подарками. Сканирует глобальный фид листингов, принимает решение о покупке по настраиваемым гейтам (скидка к флору, баланс, trade-lock, плотность рынка, редкость) и умеет автоматически перепродавать купленные лоты с поддержкой ручного вмешательства из Telegram.

> **Примечание.** Раньше в этом README был базовый пример из неофициальной документации MRKT API. Он устарел — реальная архитектура ушла далеко вперёд. Базовый пример перенесён в конец документа для справки.

---

## Оглавление

- [Что делает бот](#что-делает-бот)
- [Требования](#требования)
- [Быстрый старт](#быстрый-старт)
- [Структура проекта](#структура-проекта)
- [Как работает основной цикл](#как-работает-основной-цикл)
- [Стратегии покупки](#стратегии-покупки)
- [Гейты (фильтры) перед покупкой](#гейты-фильтры-перед-покупкой)
- [Движок автопродажи](#движок-автопродажи)
- [Telegram-управление](#telegram-управление)
- [Состояние и персистентность](#состояние-и-персистентность)
- [Логирование и диагностика](#логирование-и-диагностика)
- [Известные ограничения и технический долг](#известные-ограничения-и-технический-долг)
- [Базовый пример MRKT API](#базовый-пример-mrkt-api)

---

## Что делает бот

1. **Сканирует** глобальный фид MRKT (`POST /api/v1/gifts/saling` с `ordering=None, count=100`) примерно раз в секунду и подтягивает floor-цены всех отслеживаемых коллекций.
2. **Фильтрует** новые листинги по ряду правил (см. раздел про гейты) и принимает решение о покупке.
3. **Покупает** выбранный лот (`POST /api/v1/gifts/buy`), сразу уходит в паузу и сохраняет покупку в SQLite (`autosell.sqlite3`).
4. **Уведомляет** в Telegram об успехе (с причиной покупки, ценой, скидкой, редкостью и ID подарка).
5. **Автопродаёт** купленные лоты: ставит первоначальную цену, периодически релистит по floor, шлёт критические prompt-ы в ТГ при застое или достижении лимита изменений цены.
6. Управляется из Telegram через inline-меню: пороги скидок, баланс, толглы для всех стратегий, статистика промахов.

Поддерживаются **три независимые стратегии покупки**, которые можно включать/выключать по отдельности:

- **Обычный floor-снайпер** — ниже флора на заданный % (по бакетам цены).
- **Rare-model premium** — редкие модели с премией к флору в пределах лимита.
- **Rare-model below-floor force-buy** — редкие модели ниже флора покупаем безотносительно обычных порогов скидки.

Отдельно работает **движок автопродажи** (`AutoSellEngine`), полностью развязанный с циклом покупки.

---

## Требования

- Python **3.10+** (используется синтаксис `X | None`, `dict[K, V]` без `from __future__`)
- Windows/Linux (на Windows поддерживается звуковой алерт через `winsound`)
- Telegram-аккаунт с api_id/api_hash от [my.telegram.org](https://my.telegram.org/auth)
- Telegram-бот с токеном (через [@BotFather](https://t.me/BotFather)) — для панели управления и уведомлений

Python-зависимости (из `requirements.txt`): `aiohttp`, `aiogram`, `telethon`, `colorama`.

---

## Быстрый старт

### 1. Установка

```bash
pip install -r requirements.txt
```

### 2. Настройка `config.py`

Откройте `config.py` и заполните:

| Константа | Где взять | Для чего |
| --- | --- | --- |
| `TELEGRAM_API_ID` | [my.telegram.org](https://my.telegram.org/auth) | авто-получение токена MRKT через Telethon |
| `TELEGRAM_API_HASH` | там же | то же |
| `API_TOKEN` | можно оставить пустой — бот получит сам | токен MRKT API |
| `TELEGRAM_NOTIFY_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) | отправка уведомлений и inline-меню |
| `TELEGRAM_NOTIFY_CHAT_ID` | ваш chat_id (узнать у [@userinfobot](https://t.me/userinfobot)) | куда слать уведомления, кому разрешено пользоваться меню |

> Секреты сейчас лежат прямо в `config.py`. Перед публикацией репозитория обязательно вынесите их в переменные окружения или `.env`.

### 3. Первый запуск

```bash
python authenticator.py
```

Это запустит авторизацию через Telegram (попросит номер телефона и код). Telethon создаст файл `mrkt_telethon_session.session`. После успешного обмена будет выведен новый токен MRKT.

### 4. Запуск снайпера

```bash
python main.py
```

При старте бот:
- загрузит список коллекций и флор-цены;
- отправит приветственное уведомление в Telegram (с защитой от спама — не чаще чем раз в 15 минут при быстрых рестартах);
- выведет в консоль список отслеживаемых коллекций и начнёт скан.

---

## Структура проекта

| Файл | Назначение |
| --- | --- |
| `main.py` | главный цикл: скан → оценка → покупка → пауза |
| `scanner.py` | `Scanner` и `Listing`: запросы к `/gifts/saling`, фильтр trade-lock, расчёт скидки |
| `api.py` | `MRKTClient`: асинхронный HTTP-клиент, авто-обновление токена, ретраи на 429/5xx |
| `authenticator.py` | получение свежего токена MRKT через Telethon и `/api/v1/auth` |
| `config.py` | все константы: API, пороги скидок, гейты, настройки автопродажи |
| `state.py` | `AppState`: персистентное runtime-состояние (пороги, балансы, статистика) |
| `rare_model_strategy.py` | логика отбора редких моделей (премия/below-floor) |
| `sniper_metrics.py` | учёт промахов: "куплен раньше нас", "не хватило баланса" (по бакетам цены) |
| `autosell.py` | `AutoSellEngine`: фоновый воркер автопродажи, prompt-ы в ТГ |
| `autosell_store.py` | SQLite-хранилище лотов, purchases, critical-prompts, action log |
| `tg_controller.py` | aiogram-бот: inline-меню, команды `/menu`, `/rare`, `/sell`, `/stats`, обработка callback-ов |
| `bot.py` | цветной консольный вывод, ASCII-баннер, звуковой алерт |
| `text_normalizer.py` | эвристическая починка mojibake в текстах (см. "Известные ограничения") |
| `runtime_state.json` | персистентные поля `AppState` (создаётся автоматически) |
| `autosell.sqlite3` | БД автопродажи (создаётся автоматически) |
| `mrkt_telethon_session.session` | сессия Telethon (создаётся после `authenticator.py`) |

---

## Как работает основной цикл

```
┌─────────────────┐
│  start main()   │
└───────┬─────────┘
        │
        ▼
┌───────────────────────────────┐
│ загрузка коллекций и floor-   │
│ цен (Scanner.refresh_floor_   │
│ prices)                       │
└───────┬───────────────────────┘
        │
        ▼
┌───────────────────────────────┐
│ запуск фоновых задач:         │
│  - run_bot (tg_controller)    │
│  - AutoSellEngine.run         │
└───────┬───────────────────────┘
        │
        ▼
┌───────────────────────────────┐
│  цикл (SCAN_INTERVAL_SECONDS) │
│  1. раз в ~60 сканов refresh  │
│     floor                     │
│  2. state.is_running? нет →   │
│     sleep, иначе:             │
│  3. scan_all() → below_floor, │
│     all_new                   │
│  4. подбор rare-candidate-ов  │
│  5. звук alert, вывод         │
│  6. AUTO_BUY: попытки купить  │
│     в приоритете              │
│     rare_floor → floor → rare │
│  7. при удаче → is_running =  │
│     False, TG-уведомление     │
└───────────────────────────────┘
```

**Параметры тайминга** (все в `config.py`):

- `SCAN_INTERVAL_SECONDS = 1` — интервал скана
- `LISTINGS_PER_SCAN = 50` — сколько лотов на запрос (fallback до 20 при 400)
- `MAX_CONCURRENT_REQUESTS = 7` — семафор внутри `MRKTClient`
- `PAUSE_STATUS_LOG_INTERVAL_SECONDS = 30` — как часто логировать "на паузе"

**Refresh флора** сейчас = `max(15, round(60 / SCAN_INTERVAL_SECONDS))` сканов (см. *Известные ограничения*).

---

## Стратегии покупки

### 1. Обычный floor-снайпер (`mode = "floor"`)

Срабатывает на листингах ниже флора. Минимальная скидка зависит от цены лота:

| Цена лота | Требуемая скидка (дефолт) | Runtime-поле |
| --- | --- | --- |
| `< 4 TON` | `4.0%` | `state.discount_cheap` |
| `4-10 TON` | `3.0%` | `state.discount_expensive` (legacy) |
| `10-20 TON` | `3.0%` | `state.discount_10_20` |
| `>= 20 TON` | `3.0%` | `state.discount_20_plus` |

Порог `state.threshold = 4.0` разделяет "дешёвые" и "обычные". Пороги настраиваются через `/menu` в Telegram.

### 2. Rare-model premium (`mode = "rare_model"`)

Включается/выключается через `/rare` (после каждого рестарта принудительно OFF). Ловит редкие модели **выше флора**, если премия в пределах лимита.

- Минимальная цена: **5 TON** (жёсткий хардкод, не понижается)
- Максимальная редкость модели: `state.rare_max_rarity_percent = 1.0%`
- Премия для 5-10 TON: `state.rare_premium_5_10 = 10.0%`
- Премия для 10+ TON: `state.rare_premium_10_plus = 10.0%`
- Баланс: отдельный, `state.rare_balance = 6.0 TON`

### 3. Rare-model below-floor force-buy (`mode = "rare_model_floor"`)

Включается флагом `RARE_MODEL_BELOW_FLOOR_FORCE_BUY = True` в `config.py`. Когда rare-mode активен и лот редкой модели уже **ниже флора**, игнорируем обычные пороги скидки и покупаем его первым приоритетом.

### Порядок обработки кандидатов в одном скане

В `main.py` кандидаты складываются в один список с приоритетом:

1. `rare_model_floor` (rare + ниже флора)
2. `floor` (обычные ниже флора, исключая уже отобранные)
3. `rare_model` (rare-premium выше флора)

Максимум **3 попытки покупки на один скан** (`max_buy_attempts_per_scan`), чтобы не спамить API, но успеть на следующий лот если первый уже продан.

---

## Гейты (фильтры) перед покупкой

Гейты применяются в таком порядке для каждого кандидата:

### 1. Trade-lock filter

Определяется в `scanner.analyze_trade_lock` по полям `nextResaleDate`, `nextTransferDate`, `unlockDate`, `waitGiftUntil`, `exportDate`, `returnLockedUntil`, `validateRegularGiftAt`.

- `lock < 3 дней` → разрешено
- `3 <= lock < 10 дней` → только если `discount_percent >= 13%`
- `lock >= 10 дней` → всегда скип

Пороги: `AUTO_BUY_TRADE_BAN_*` в `config.py`. Отключается `AUTO_BUY_SKIP_TRADE_BAN = False`.

### 2. Balance gate

`listing.listing_price_ton > balance_limit` → скип.

- Для обычной стратегии — `state.balance` (дефолт `AUTO_BUY_MAX_PRICE_TON = 6.0`)
- Для rare — `state.rare_balance`

При этом считается **статистика упущенного** (только если `price > 8 TON` и `discount >= 3%`), раскладывается по бакетам `8-20`, `20-50`, `50+`, и обновляется "лучший промах за день".

### 3. Discount bucket (только для `floor`)

См. таблицу в "Обычный floor-снайпер".

### 4. Depth guard (только для `floor`, опционально)

Анти-дамп защита. Включается тумблером "Анти-дамп" в `/menu`. Алгоритм:

1. Быстрый путь — посчитать, сколько лотов в текущем фиде имеют ту же коллекцию и цену `<=` нашей.
2. Если больше `buy_depth_guard_max_at_or_below` (дефолт 3) — скип.
3. Если в фиде намёк на "толпу" (2+ лота) — делается один снимок `/gifts/saling` с ordering=Price lowToHigh для этой коллекции, и проверка повторяется на живых данных.
4. Дополнительно: если cheapest-lot ниже нашего больше чем на `buy_depth_guard_near_price_percent %` (дефолт 0.35) — скип.

Параметры в `config.py`: `AUTO_BUY_DEPTH_GUARD_*`.

### 5. Собственно вызов `buy_gift`

Bot делает `POST /api/v1/gifts/buy` с payload `{"Ids": [gift_id]}` (с fallback на `{"ids": [...]}` и `{"giftIds": [...]}` при 400).

Интерпретация ответа:

| Ответ | Статус | Действие |
| --- | --- | --- |
| `None` (сеть умерла) | ошибка | лог error |
| `[]` (пустой list) | подарок уже продан | `stat_missed_bought_before++` |
| `dict` с `error/errors` | серверная ошибка | лог error; если "already sold" — тот же счётчик |
| непустой `list` | успех | запись purchase, пауза, TG-уведомление |

---

## Движок автопродажи

`AutoSellEngine` (`autosell.py`) — **независимый фоновый воркер**, тикает каждые 5 секунд. Включается/выключается через `/sell` (после рестарта OFF).

### Жизненный цикл лота

```
purchase recorded          ┌─────────┐
───────────────────────────▶  NEW    │
                           └────┬────┘
                                │ first sale posted
                                ▼
                           ┌─────────┐
         relist (change-   │ LISTED  │
         price) по расп.   └────┬────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
      ┌──────────────┐ ┌──────────────┐  ┌──────────────┐
      │ stuck >= 2ч  │ │ relist_count │  │ gift исчез   │
      │ & price >=   │ │ >= limit     │  │ из inventory │
      │ prompt_      │ └──────┬───────┘  │ 4 цикла      │
      │ threshold    │        │          └──────┬───────┘
      └──────┬───────┘        │                 ▼
             │                │           ┌──────────┐
             ▼                ▼           │   HOLD   │
       ┌──────────────────────────┐       └──────────┘
       │      WAIT_PROMPT         │
       │  (ждём решения из ТГ     │
       │   или AUTO_TIMEOUT →     │
       │   hold)                  │
       └──────┬───────────────────┘
              │
              ▼ (действия: hold / sell_now / buy5 / extend:N)
       LISTED / HOLD / SOLD
```

### Расчёт цены

```
target_profit  = 2% для 5-10 TON, 2% для 10+ TON
floor_premium  = 1.5% для 5-10 TON, 1.0% для 10+ TON
buy_target     = buy_price × (1 + target_profit)
floor_target   = floor × (1 + floor_premium)
desired_price  = max(buy_target, floor_target, stop_loss_price)
stop_loss      = buy_price × (1 - loss_cap_percent%), дефолт -4%
```

### Критические prompt-ы

Прилетают в Telegram с inline-кнопками:

| Кнопка | Действие |
| --- | --- |
| **Холд** | статус → HOLD, ничего не делаем |
| **Продать сейчас** | если `order_mode_critical_only` ON — пробуем `fill_order` по лучшему bid-у (если bid >= stop_loss); иначе релист с минимальной ценой |
| **Докупить +5** | `POST /gifts/sell-by-spice`, `extra_changes += 5` |
| **30м / 1ч / 2ч / 6ч** | `next_critical_at = now + N` минут |
| **Ввести своё время** | FSM ждёт число минут |

Если пользователь не ответил в течение `AUTO_SELL_CRITICAL_PROMPT_TIMEOUT_SECONDS = 900` (15 минут) — применяется `default_on_timeout = "hold"`.

### Лимит изменений цены

`allowed_price_change_limit(extra_changes) = 5 + extra_changes`.

MRKT даёт 5 бесплатных смен цены, дальше `buy5` покупает пакет из +5 за spice (0.1 TON). После N релистов (где N = этот лимит) движок шлёт критический prompt `relist_limit_reached`.

> В планах — снизить базовое число до `4` (см. спек `.kiro/specs/sniper-reliability-improvements/`).

### Rare-protect

Если `model_rarity_percent <= sell_rare_protect_percent` (дефолт 1.0%) — лот автоматически уходит в HOLD и не продаётся авто. Решение всегда ручное.

### Sync с инвентарём

Каждые 20 секунд воркер тянет `/api/v1/gifts` (фильтры `isListed=True/False`, по 20 за страницу, обходит курсором до 20 страниц). Это делается **последовательно**, блокирует один слот семафора.

Чтобы не считать подарок проданным из-за нестабильности эндпоинта, реализован буфер `MISSING_INVENTORY_CONFIRM_CYCLES = 4` — лот переходит в HOLD только если его нет в инвентаре 4 цикла подряд.

---

## Telegram-управление

Доступно только из чата с `TELEGRAM_NOTIFY_CHAT_ID` (проверка `is_allowed_chat`).

### Команды

| Команда | Что показывает |
| --- | --- |
| `/start`, `/menu` | главное меню снайпера |
| `/rare` | меню rare-model стратегии |
| `/sell` | меню автопродажи |
| `/stats` | статистика промахов снайпера |

### Главное меню (`/menu`)

- **Статус: ON/OFF** — тумблер скана (`state.is_running`)
- **Баланс** — ввод баланса обычной стратегии
- **Скидка (`<4T`, `>=4T`, `10-20T`, `20T+`)** — пороги по бакетам
- **Анти-дамп: ON/OFF** — тумблер depth guard
- **Тест: выставить за 30 TON** — тестовое выставление первого доступного подарка (⚠ без подтверждения)
- **Статистика снайпера** — показывает то же, что `/stats`
- **Меню редких моделей** → `/rare`
- **Меню автопродажи** → `/sell`

### Меню редких моделей (`/rare`)

- **Режим редких моделей: ON/OFF** (после рестарта всегда OFF)
- **Баланс rare**
- **Premium 5-10T / 10+T** — максимальная премия к флору
- Статичные индикаторы: мин. цена (5 TON), порог редкости (1.0%)

### Меню автопродажи (`/sell`)

- **Автопродажа: ON/OFF** (после рестарта всегда OFF)
- **Баланс автопродажи** — не используется для логики продажи, отображение-only
- **Профит 5-10T / 10+T** — целевая маржа
- **Премия к флору 5-10T / 10+T**
- **Релист** — интервал в минутах
- **Горизонт** — через сколько часов считать лот застрявшим
- **Лимит убытка** — stop-loss в %
- **Критика при цене >=** — ниже какой цены не шлём prompt (просто HOLD молча)
- **Защита редкости** — порог, ниже которого авто не продаём
- **Режим ордеров** — при "Продать сейчас" пробовать ли `fill_order`

### Статистика (`/stats`)

- **Упущено: лот уже купили до нас** — счётчик случаев, когда `buy_gift` вернул пусто
- **Упущено из-за баланса** — суммарно и по бакетам `8-20 / 20-50 / 50+ TON`
- **Лучший пропущенный за день** — максимальная скидка лота, который не прошёл balance gate

Все значения персистентны между перезапусками (`runtime_state.json`).

---

## Состояние и персистентность

### `runtime_state.json`

Сериализуется `AppState.save()` при каждой модификации "отслеживаемого" поля (см. `MUTABLE_FIELDS` в `state.py`). Атомарная запись: сначала в `.json.tmp`, потом `replace`.

При старте значения загружаются и **накладываются поверх дефолтов из `config.py`**. Неизвестные в новой версии поля игнорируются, отсутствующие берут дефолты — это обеспечивает backward compat при роллбэке.

Особые инварианты при старте:
- `MAIN_SNIPER_FORCE_RUNNING_ON_START = True` → `is_running = True` даже если в файле `False`
- `rare_enabled` принудительно `False`
- `sell_enabled` принудительно `False`

### `autosell.sqlite3`

Схема (`autosell_store._init_db`):

- **`purchases`** — все совершённые покупки (`gift_id` PK, цена, коллекция, модель, timestamp)
- **`sell_lots`** — активные и прошлые лоты автопродажи (статус NEW / LISTED / WAIT_PROMPT / HOLD / SOLD)
- **`critical_prompts`** — очередь prompt-ов с дедлайнами, разрешёнными действиями, флагом обработки
- **`sell_actions_log`** — audit-лог всех действий движка и пользовательских ответов

`PRAGMA journal_mode=WAL` для безопасных параллельных чтений.

### `mrkt_telethon_session.session`

Файл сессии Telethon. При утере — нужен повторный логин через `authenticator.py`. Не переносить на другую машину без выхода из аккаунта, Telegram считает это подозрительным.

### `startup_notify_state.json`

Хранит `last_startup_notify_ts` — защита от спама "Bot started" при рестарт-лупе (cooldown 15 минут).

---

## Логирование и диагностика

### Формат

```
<timestamp> [<logger-name>] <LEVEL>: <message>
```

Уровни: `INFO` по умолчанию, `DEBUG` при `VERBOSE = True` в `config.py`.

### Ключевые логгеры

- `mrkt.main` — главный цикл, покупки, статистика
- `mrkt.scanner` — загрузка коллекций и фида
- `mrkt.api` — HTTP-запросы (BUY REQUEST / BUY RESPONSE, 401/429/timeout)
- `mrkt.autosell` — действия движка автопродажи
- `mrkt.auth` — обновление токена
- `mrkt.tg_bot` — Telegram-контроллер

### Консольный вывод

`bot.py` рисует ASCII-баннер при старте, цветные блоки для below-floor deals, сводку по каждому скану (`X новых листингов | Y НИЖЕ ФЛОРА`). Звуковой алерт (Windows only): `winsound.Beep` при любом сигнале.

### Запросы на покупку

Каждая попытка покупки логируется в `INFO`:
```
BUY REQUEST: POST /api/v1/gifts/buy | gift_id=... | payload_variant=1 keys=['Ids']
BUY RESPONSE: [...]
```

Полезно для post-mortem анализа "почему мы не купили этот лот".

---

## Известные ограничения и технический долг

> Не все баги — критические. Список отсортирован по приоритету.

### 🔴 Критично

1. **Секреты в `config.py`.** `API_TOKEN`, `TELEGRAM_API_HASH`, `TELEGRAM_NOTIFY_BOT_TOKEN`, `TELEGRAM_NOTIFY_CHAT_ID` вшиты в исходник. Перед публикацией репозитория — вынести в `.env` + `python-dotenv`.
2. **Mojibake в исходниках.** Значительная часть русских строк в `main.py` и `tg_controller.py` хранится как "ломаный cp1251 в UTF-8" (`"РџР°РЅРµР»СЊ"`). `text_normalizer.normalize_text` на лету переконвертирует текст перед отправкой в Telegram, но сами файлы так не починишь — нужен полный sweep с перекодировкой.
3. **Floor обновляется раз в ~60 сканов (≈1 мин).** При `SCAN_INTERVAL_SECONDS=1` это очень долго — `discount_percent` считается по устаревшему floor, что даёт и ложные, и упущенные сигналы. Запланировано снижение до ~12 секунд (см. спек `sniper-reliability-improvements`).
4. **Scanner читает только 100 последних листингов.** Если за скан появилось больше новых лотов (или сеть задержалась), ниже-флоровый лот мог вылететь из окна. Дополнительный проход с `ordering="Price", lowToHigh=True` по горячим коллекциям закрыл бы дыру.
5. **Trade-lock валидируется только по данным из глобального фида.** Не все поля (`nextResaleDate` и т.п.) гарантированно возвращаются в `/gifts/saling`. В редких случаях возможна покупка лота, который через минуту нельзя перепродать.

### 🟡 Важно

6. **`winsound.Beep` блокирует event loop ~700 мс.** Каждый below-floor сигнал съедает пол-скана. Решение: `asyncio.to_thread(winsound.Beep, ...)` или вообще выключить на VPS.
7. **`state.save()` на каждом `__setattr__`.** Инкременты статистики промахов вызывают запись всего JSON десятки раз в секунду. Нужен throttled flush или отдельный stats store.
8. **Нет супервизии фоновых задач.** `tg_task` и `autosell_task` создаются через `asyncio.create_task`, без `add_done_callback` с перезапуском. Если одна упадёт — тихо.
9. **Гонка при 401-шторме.** 7 конкурентных запросов одновременно могут инициировать refresh токена через Telethon. Нужен `asyncio.Lock` на `_update_token`.
10. **Off-by-one в лимите изменений цены автопродажи.** `allowed_price_change_limit = 5 + extra_changes`, а MRKT даёт **4** бесплатных. Фикс запланирован в `sniper-reliability-improvements`.
11. **FSM aiogram в памяти.** При рестарте бота FSM-состояния теряются, пользователь может залипнуть в "ожидании ввода". Нужен persistent storage.

### 🟢 Мелочи

12. Кнопка **"Тест выставить за 30 TON"** — без подтверждения. Случайный тык может продать лот под флор. Стоит добавить confirm с текущим floor.
13. **`Scanner._scan_collection`** больше не используется (в `scan_all` только global feed), но остался в коде.
14. **`MIN_DISCOUNT_PERCENT=2%` vs `AUTO_BUY_MIN_DISCOUNT_CHEAP=4%`** — при 3% скидке лот подсвечивается как below-floor и пищит winsound, но купить его нельзя. Визуальный шум.
15. **Payload `get_my_gifts`** содержит дублирующиеся ключи (`lowToHigh`, `luckyBuy` по два раза в одном dict). Python берёт последнее — работает, но некрасиво.

### ⚪ Deferred (будущие фичи)

- **Monochrome detector** — автоматическое определение "монохромных" сочетаний модели и фона через бесплатный vision-API (Gemini Flash / Cloudflare Workers AI) с локальным кэшем. Слишком медленно для hot path, но подходит для background-воркера.
- **Backtest engine** — сохранение сырых ответов `/gifts/saling` в jsonl для проигрывания новых стратегий оффлайн.
- **Daily P/L summary** — сводка по сделкам в ТГ раз в сутки.
- **Kill-switch по совокупному убытку** — пауза бота при накопленном минусе за день.

---

## Базовый пример MRKT API

Для справки — минимальный рабочий пример получения токена и скана листингов из неофициальной документации MRKT.

```python
import asyncio
from pyrogram import Client
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName, InputUser
from urllib.parse import unquote
from curl_cffi import requests

MARKET_API_URL = 'https://api.tgmrkt.io/api/v1'
api_id = 123456  # my.telegram.org
api_hash = 'YOUR_HASH'
client = Client('main', api_id, api_hash)


async def get_auth_token():
    async with client:
        bot_entity = await client.get_users('mrkt')
        peer = await client.resolve_peer('mrkt')
        bot = InputUser(user_id=bot_entity.id, access_hash=bot_entity.raw.access_hash)
        bot_app = InputBotAppShortName(bot_id=bot, short_name='app')
        web_view = await client.invoke(RequestAppWebView(
            peer=peer, app=bot_app, platform='android',
        ))
        init_data = unquote(
            web_view.url.split('tgWebAppData=', 1)[1].split('&tgWebAppVersion', 1)[0]
        )
        r = requests.post(
            url=f'{MARKET_API_URL}/auth',
            json={'data': init_data},
        )
        return r.json().get('token')


async def main():
    token = await get_auth_token()
    headers = {'Authorization': token, 'Referer': 'https://cdn.tgmrkt.io/'}
    json_data = {
        'collectionNames': ['Lunar Snake'],
        'modelNames': ['Albino'],
        'backdropNames': [],
        'symbolNames': [],
        'ordering': 'Price',
        'lowToHigh': True,
        'count': 20,
        'cursor': '',
    }
    r = requests.post(f'{MARKET_API_URL}/gifts/saling', headers=headers, json=json_data)
    print(r.json().get('gifts'))


asyncio.run(main())
```

В этом репозитории этот код сильно расширен — отдельно см. `api.py`, `authenticator.py`, `scanner.py`.

### Основные эндпоинты MRKT, используемые ботом

| Method | Endpoint | Назначение |
| --- | --- | --- |
| `POST` | `/api/v1/auth` | обмен `tgWebAppData` на токен |
| `GET` | `/api/v1/gifts/collections` | список коллекций + `floorPriceNanoTons` |
| `POST` | `/api/v1/gifts/saling` | активные листинги (фид и поколлекционный) |
| `POST` | `/api/v1/gifts/buy` | покупка по id |
| `POST` | `/api/v1/gifts` | свой инвентарь (с фильтром `isListed`) |
| `POST` | `/api/v1/gifts/sale` | выставить на продажу |
| `POST` | `/api/v1/gifts/sale/change-price` | изменить цену листинга |
| `POST` | `/api/v1/gifts/sale/cancel` | снять с продажи |
| `POST` | `/api/v1/gifts/sell-by-spice` | купить +5 смен цены за spice |
| `POST` | `/api/v1/orders/top` | топ-bid по коллекции/модели |
| `POST` | `/api/v1/orders` | глубина ордеров |
| `GET` | `/api/v1/orders/all-collection-top` | топ-bid по всем коллекциям |
| `POST` | `/api/v1/orders/fill/` | мгновенное исполнение ордера |
| `POST` | `/api/v1/orders/get-my-orders` | свои ордера |

---

## Лицензия

Используйте на свой страх и риск. Бот работает с реальным балансом TON — любой баг в логике покупки/продажи может привести к потере средств. Тестируйте изменения на маленьком балансе.
