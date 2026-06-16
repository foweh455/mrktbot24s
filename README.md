# MRKT NFT Gift Sniper Bot

An asynchronous sniper bot for [MRKT](https://t.me/mrkt) — a marketplace for trading Telegram NFT gifts. It scans the global listing feed, makes buy decisions based on configurable gates (discount to floor, balance, trade-lock, market depth, rarity), and can automatically resell purchased lots with support for manual intervention from Telegram.

> **Note.** This README used to contain a basic example from the unofficial MRKT API docs. It's outdated — the real architecture has moved far beyond it. The basic example has been moved to the end of the document for reference.

---

## Table of Contents

- [What the bot does](#what-the-bot-does)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Project structure](#project-structure)
- [How the main loop works](#how-the-main-loop-works)
- [Buy strategies](#buy-strategies)
- [Pre-purchase gates (filters)](#pre-purchase-gates-filters)
- [Auto-sell engine](#auto-sell-engine)
- [Telegram control](#telegram-control)
- [State and persistence](#state-and-persistence)
- [Logging and diagnostics](#logging-and-diagnostics)
- [Known limitations and technical debt](#known-limitations-and-technical-debt)
- [Basic MRKT API example](#basic-mrkt-api-example)

---

## What the bot does

1. **Scans** the global MRKT feed (`POST /api/v1/gifts/saling` with `ordering=None, count=100`) roughly once per second and pulls floor prices for all tracked collections.
2. **Filters** new listings through a set of rules (see the gates section) and makes a buy decision.
3. **Buys** the selected lot (`POST /api/v1/gifts/buy`), immediately pauses, and saves the purchase to SQLite (`autosell.sqlite3`).
4. **Notifies** Telegram on success (with the buy reason, price, discount, rarity, and gift ID).
5. **Auto-sells** purchased lots: sets the initial price, periodically relists at floor, and sends critical prompts to Telegram when a lot is stuck or the price-change limit is reached.
6. Is controlled from Telegram via inline menus: discount thresholds, balance, toggles for all strategies, miss statistics.

It supports **three independent buy strategies** that can be toggled on/off individually:

- **Regular floor sniper** — below floor by a configured % (per price bucket).
- **Rare-model premium** — rare models with a premium over floor within a limit.
- **Rare-model below-floor force-buy** — rare models below floor are bought regardless of the regular discount thresholds.

Separately, an **auto-sell engine** (`AutoSellEngine`) runs fully decoupled from the buy loop.

---

## Requirements

- Python **3.10+** (uses the `X | None`, `dict[K, V]` syntax without `from __future__`)
- Windows/Linux (on Windows a sound alert via `winsound` is supported)
- A Telegram account with api_id/api_hash from [my.telegram.org](https://my.telegram.org/auth)
- A Telegram bot with a token (via [@BotFather](https://t.me/BotFather)) — for the control panel and notifications

Python dependencies (from `requirements.txt`): `aiohttp`, `aiogram`, `telethon`, `colorama`.

---

## Quick start

### 1. Installation

```bash
pip install -r requirements.txt
```

### 2. Configuration (`config.py` / `.env`)

Secrets are read from environment variables. Copy `.env.example` to `.env` and fill in:

| Variable | Where to get it | What it's for |
| --- | --- | --- |
| `TELEGRAM_API_ID` | [my.telegram.org](https://my.telegram.org/auth) | auto-fetching the MRKT token via Telethon |
| `TELEGRAM_API_HASH` | same place | same |
| `API_TOKEN` | can be left empty — the bot fetches it itself | MRKT API token |
| `TELEGRAM_NOTIFY_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) | sending notifications and the inline menu |
| `TELEGRAM_NOTIFY_CHAT_ID` | your chat_id (find it via [@userinfobot](https://t.me/userinfobot)) | where to send notifications and who is allowed to use the menu |

> Never commit real secrets. `config.py` loads them from the environment (via `python-dotenv`); keep your `.env` out of git (it's already in `.gitignore`).

### 3. First run

```bash
python authenticator.py
```

This starts authorization via Telegram (it asks for your phone number and a code). Telethon creates the `mrkt_telethon_session.session` file. After a successful exchange, a new MRKT token is printed.

### 4. Running the sniper

```bash
python main.py
```

On startup the bot:
- loads the list of collections and floor prices;
- sends a welcome notification to Telegram (with anti-spam protection — no more than once every 15 minutes during fast restarts);
- prints the list of tracked collections to the console and starts scanning.

---

## Project structure

| File | Purpose |
| --- | --- |
| `main.py` | main loop: scan → evaluate → buy → pause |
| `scanner.py` | `Scanner` and `Listing`: requests to `/gifts/saling`, trade-lock filter, discount calculation |
| `api.py` | `MRKTClient`: async HTTP client, auto token refresh, retries on 429/5xx |
| `authenticator.py` | fetching a fresh MRKT token via Telethon and `/api/v1/auth` |
| `config.py` | all constants: API, discount thresholds, gates, auto-sell settings |
| `state.py` | `AppState`: persistent runtime state (thresholds, balances, statistics) |
| `rare_model_strategy.py` | rare-model selection logic (premium/below-floor) |
| `sniper_metrics.py` | miss tracking: "bought before us", "not enough balance" (per price bucket) |
| `autosell.py` | `AutoSellEngine`: background auto-sell worker, Telegram prompts |
| `autosell_store.py` | SQLite storage for lots, purchases, critical-prompts, action log |
| `tg_controller.py` | aiogram bot: inline menu, `/menu`, `/rare`, `/sell`, `/stats` commands, callback handling |
| `bot.py` | colored console output, ASCII banner, sound alert |
| `text_normalizer.py` | heuristic mojibake repair in text (see "Known limitations") |
| `runtime_state.json` | persistent `AppState` fields (created automatically) |
| `autosell.sqlite3` | auto-sell DB (created automatically) |
| `mrkt_telethon_session.session` | Telethon session (created after `authenticator.py`) |

---

## How the main loop works

```
┌─────────────────┐
│  start main()   │
└───────┬─────────┘
        │
        ▼
┌───────────────────────────────┐
│ load collections and floor    │
│ prices (Scanner.refresh_floor_│
│ prices)                       │
└───────┬───────────────────────┘
        │
        ▼
┌───────────────────────────────┐
│ start background tasks:       │
│  - run_bot (tg_controller)    │
│  - AutoSellEngine.run         │
└───────┬───────────────────────┘
        │
        ▼
┌───────────────────────────────┐
│  loop (SCAN_INTERVAL_SECONDS) │
│  1. ~every 60 scans, refresh  │
│     floor                     │
│  2. state.is_running? no →    │
│     sleep, otherwise:         │
│  3. scan_all() → below_floor, │
│     all_new                   │
│  4. pick rare candidates      │
│  5. sound alert, output       │
│  6. AUTO_BUY: buy attempts    │
│     in priority order         │
│     rare_floor → floor → rare │
│  7. on success → is_running = │
│     False, TG notification    │
└───────────────────────────────┘
```

**Timing parameters** (all in `config.py`):

- `SCAN_INTERVAL_SECONDS = 1` — scan interval
- `LISTINGS_PER_SCAN = 50` — how many lots per request (falls back to 20 on a 400)
- `MAX_CONCURRENT_REQUESTS = 7` — semaphore inside `MRKTClient`
- `PAUSE_STATUS_LOG_INTERVAL_SECONDS = 30` — how often to log "paused"

**Floor refresh** is currently `max(15, round(60 / SCAN_INTERVAL_SECONDS))` scans (see *Known limitations*).

---

## Buy strategies

### 1. Regular floor sniper (`mode = "floor"`)

Triggers on listings below floor. The minimum discount depends on the lot price:

| Lot price | Required discount (default) | Runtime field |
| --- | --- | --- |
| `< 4 TON` | `4.0%` | `state.discount_cheap` |
| `4-10 TON` | `3.0%` | `state.discount_expensive` (legacy) |
| `10-20 TON` | `3.0%` | `state.discount_10_20` |
| `>= 20 TON` | `3.0%` | `state.discount_20_plus` |

The `state.threshold = 4.0` value splits "cheap" from "regular". Thresholds are configured via `/menu` in Telegram.

### 2. Rare-model premium (`mode = "rare_model"`)

Toggled via `/rare` (forced OFF after every restart). Catches rare models **above floor** if the premium is within the limit.

- Minimum price: **5 TON** (hard-coded, not lowered)
- Maximum model rarity: `state.rare_max_rarity_percent = 1.0%`
- Premium for 5-10 TON: `state.rare_premium_5_10 = 10.0%`
- Premium for 10+ TON: `state.rare_premium_10_plus = 10.0%`
- Balance: separate, `state.rare_balance = 6.0 TON`

### 3. Rare-model below-floor force-buy (`mode = "rare_model_floor"`)

Enabled via the `RARE_MODEL_BELOW_FLOOR_FORCE_BUY = True` flag in `config.py`. When rare mode is active and a rare-model lot is already **below floor**, the regular discount thresholds are ignored and it's bought with top priority.

### Candidate processing order within a single scan

In `main.py` candidates are collected into one list with this priority:

1. `rare_model_floor` (rare + below floor)
2. `floor` (regular below-floor, excluding already-selected ones)
3. `rare_model` (rare-premium above floor)

A maximum of **3 buy attempts per scan** (`max_buy_attempts_per_scan`), so the API isn't spammed but we can still catch the next lot if the first is already sold.

---

## Pre-purchase gates (filters)

The gates are applied in this order for each candidate:

### 1. Trade-lock filter

Determined in `scanner.analyze_trade_lock` from the `nextResaleDate`, `nextTransferDate`, `unlockDate`, `waitGiftUntil`, `exportDate`, `returnLockedUntil`, `validateRegularGiftAt` fields.

- `lock < 3 days` → allowed
- `3 <= lock < 10 days` → only if `discount_percent >= 13%`
- `lock >= 10 days` → always skip

Thresholds: `AUTO_BUY_TRADE_BAN_*` in `config.py`. Disabled via `AUTO_BUY_SKIP_TRADE_BAN = False`.

### 2. Balance gate

`listing.listing_price_ton > balance_limit` → skip.

- For the regular strategy — `state.balance` (default `AUTO_BUY_MAX_PRICE_TON = 6.0`)
- For rare — `state.rare_balance`

When this happens, **missed-opportunity statistics** are tracked (only if `price > 8 TON` and `discount >= 3%`), bucketed into `8-20`, `20-50`, `50+`, and the "best miss of the day" is updated.

### 3. Discount bucket (only for `floor`)

See the table in "Regular floor sniper".

### 4. Depth guard (only for `floor`, optional)

Anti-dump protection. Toggled via "Anti-dump" in `/menu`. Algorithm:

1. Fast path — count how many lots in the current feed have the same collection and a price `<=` ours.
2. If more than `buy_depth_guard_max_at_or_below` (default 3) — skip.
3. If the feed hints at a "crowd" (2+ lots) — take a single `/gifts/saling` snapshot with ordering=Price lowToHigh for that collection and re-check against live data.
4. Additionally: if the cheapest lot below ours is more than `buy_depth_guard_near_price_percent %` (default 0.35) below — skip.

Parameters in `config.py`: `AUTO_BUY_DEPTH_GUARD_*`.

### 5. The actual `buy_gift` call

The bot does `POST /api/v1/gifts/buy` with payload `{"Ids": [gift_id]}` (with fallback to `{"ids": [...]}` and `{"giftIds": [...]}` on a 400).

Response interpretation:

| Response | Status | Action |
| --- | --- | --- |
| `None` (network died) | error | log error |
| `[]` (empty list) | gift already sold | `stat_missed_bought_before++` |
| `dict` with `error/errors` | server error | log error; if "already sold" — same counter |
| non-empty `list` | success | record purchase, pause, TG notification |

---

## Auto-sell engine

`AutoSellEngine` (`autosell.py`) is an **independent background worker** that ticks every 5 seconds. Toggled via `/sell` (OFF after restart).

### Lot lifecycle

```
purchase recorded          ┌─────────┐
───────────────────────────▶  NEW    │
                           └────┬────┘
                                │ first sale posted
                                ▼
                           ┌─────────┐
         relist (change-   │ LISTED  │
         price) on sched.  └────┬────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
      ┌──────────────┐ ┌──────────────┐  ┌──────────────┐
      │ stuck >= 2h  │ │ relist_count │  │ gift gone    │
      │ & price >=   │ │ >= limit     │  │ from inventory│
      │ prompt_      │ └──────┬───────┘  │ 4 cycles     │
      │ threshold    │        │          └──────┬───────┘
      └──────┬───────┘        │                 ▼
             │                │           ┌──────────┐
             ▼                ▼           │   HOLD   │
       ┌──────────────────────────┐       └──────────┘
       │      WAIT_PROMPT         │
       │  (wait for a decision    │
       │   from TG or AUTO_       │
       │   TIMEOUT → hold)        │
       └──────┬───────────────────┘
              │
              ▼ (actions: hold / sell_now / buy5 / extend:N)
       LISTED / HOLD / SOLD
```

### Price calculation

```
target_profit  = 2% for 5-10 TON, 2% for 10+ TON
floor_premium  = 1.5% for 5-10 TON, 1.0% for 10+ TON
buy_target     = buy_price × (1 + target_profit)
floor_target   = floor × (1 + floor_premium)
desired_price  = max(buy_target, floor_target, stop_loss_price)
stop_loss      = buy_price × (1 - loss_cap_percent%), default -4%
```

### Critical prompts

These arrive in Telegram with inline buttons:

| Button | Action |
| --- | --- |
| **Hold** | status → HOLD, do nothing |
| **Sell now** | if `order_mode_critical_only` is ON — try `fill_order` against the best bid (if bid >= stop_loss); otherwise relist at the minimum price |
| **Buy +5** | `POST /gifts/sell-by-spice`, `extra_changes += 5` |
| **30m / 1h / 2h / 6h** | `next_critical_at = now + N` minutes |
| **Enter custom time** | FSM waits for a number of minutes |

If the user doesn't respond within `AUTO_SELL_CRITICAL_PROMPT_TIMEOUT_SECONDS = 900` (15 minutes), `default_on_timeout = "hold"` is applied.

### Price-change limit

`allowed_price_change_limit(extra_changes) = 5 + extra_changes`.

MRKT gives 5 free price changes; beyond that, `buy5` purchases a pack of +5 for spice (0.1 TON). After N relists (where N = this limit) the engine sends a `relist_limit_reached` critical prompt.

> Planned — lower the base number to `4` (see the `.kiro/specs/sniper-reliability-improvements/` spec).

### Rare-protect

If `model_rarity_percent <= sell_rare_protect_percent` (default 1.0%), the lot automatically goes to HOLD and is not auto-sold. The decision is always manual.

### Inventory sync

Every 20 seconds the worker pulls `/api/v1/gifts` (filters `isListed=True/False`, 20 per page, paging by cursor up to 20 pages). This is done **sequentially** and blocks one semaphore slot.

To avoid marking a gift as sold due to endpoint instability, a `MISSING_INVENTORY_CONFIRM_CYCLES = 4` buffer is implemented — a lot moves to HOLD only if it's missing from inventory for 4 cycles in a row.

---

## Telegram control

Available only from the chat with `TELEGRAM_NOTIFY_CHAT_ID` (the `is_allowed_chat` check).

### Commands

| Command | What it shows |
| --- | --- |
| `/start`, `/menu` | the sniper's main menu |
| `/rare` | rare-model strategy menu |
| `/sell` | auto-sell menu |
| `/stats` | sniper miss statistics |

### Main menu (`/menu`)

- **Status: ON/OFF** — scan toggle (`state.is_running`)
- **Balance** — set the regular strategy's balance
- **Discount (`<4T`, `>=4T`, `10-20T`, `20T+`)** — per-bucket thresholds
- **Anti-dump: ON/OFF** — depth-guard toggle
- **Test: list for 30 TON** — test-list the first available gift (⚠ no confirmation)
- **Sniper statistics** — shows the same as `/stats`
- **Rare models menu** → `/rare`
- **Auto-sell menu** → `/sell`

### Rare models menu (`/rare`)

- **Rare model mode: ON/OFF** (always OFF after restart)
- **Rare balance**
- **Premium 5-10T / 10+T** — maximum premium over floor
- Static indicators: min price (5 TON), rarity threshold (1.0%)

### Auto-sell menu (`/sell`)

- **Auto-sell: ON/OFF** (always OFF after restart)
- **Auto-sell balance** — not used for sell logic, display-only
- **Profit 5-10T / 10+T** — target margin
- **Floor premium 5-10T / 10+T**
- **Relist** — interval in minutes
- **Horizon** — after how many hours a lot is considered stuck
- **Loss cap** — stop-loss in %
- **Critical at price >=** — below which price we don't send a prompt (just HOLD silently)
- **Rarity protection** — threshold below which we don't auto-sell
- **Order mode** — whether to try `fill_order` on "Sell now"

### Statistics (`/stats`)

- **Missed: lot bought before us** — counter for cases where `buy_gift` returned empty
- **Missed due to balance** — total and per bucket `8-20 / 20-50 / 50+ TON`
- **Best miss of the day** — the maximum discount of a lot that didn't pass the balance gate

All values are persistent across restarts (`runtime_state.json`).

---

## State and persistence

### `runtime_state.json`

Serialized by `AppState.save()` on every modification of a "tracked" field (see `MUTABLE_FIELDS` in `state.py`). Atomic write: first to `.json.tmp`, then `replace`.

On startup, values are loaded and **layered on top of the defaults from `config.py`**. Fields unknown in the new version are ignored, missing ones take defaults — this provides backward compatibility on rollback.

Special startup invariants:
- `MAIN_SNIPER_FORCE_RUNNING_ON_START = True` → `is_running = True` even if the file says `False`
- `rare_enabled` forced to `False`
- `sell_enabled` forced to `False`

### `autosell.sqlite3`

Schema (`autosell_store._init_db`):

- **`purchases`** — all completed purchases (`gift_id` PK, price, collection, model, timestamp)
- **`sell_lots`** — active and past auto-sell lots (status NEW / LISTED / WAIT_PROMPT / HOLD / SOLD)
- **`critical_prompts`** — queue of prompts with deadlines, allowed actions, and a processed flag
- **`sell_actions_log`** — audit log of all engine actions and user responses

`PRAGMA journal_mode=WAL` for safe concurrent reads.

### `mrkt_telethon_session.session`

The Telethon session file. If lost, you need to re-login via `authenticator.py`. Don't move it to another machine without logging out of the account — Telegram treats that as suspicious.

### `startup_notify_state.json`

Stores `last_startup_notify_ts` — protection against "Bot started" spam during a restart loop (15-minute cooldown).

---

## Logging and diagnostics

### Format

```
<timestamp> [<logger-name>] <LEVEL>: <message>
```

Levels: `INFO` by default, `DEBUG` when `VERBOSE = True` in `config.py`.

### Key loggers

- `mrkt.main` — main loop, purchases, statistics
- `mrkt.scanner` — loading collections and the feed
- `mrkt.api` — HTTP requests (BUY REQUEST / BUY RESPONSE, 401/429/timeout)
- `mrkt.autosell` — auto-sell engine actions
- `mrkt.auth` — token refresh
- `mrkt.tg_bot` — Telegram controller

### Console output

`bot.py` draws an ASCII banner at startup, colored blocks for below-floor deals, and a per-scan summary (`X new listings | Y BELOW FLOOR`). Sound alert (Windows only): `winsound.Beep` on any signal.

### Buy requests

Every buy attempt is logged at `INFO`:
```
BUY REQUEST: POST /api/v1/gifts/buy | gift_id=... | payload_variant=1 keys=['Ids']
BUY RESPONSE: [...]
```

Useful for post-mortem analysis of "why didn't we buy that lot".

---

## Known limitations and technical debt

> Not all bugs are critical. The list is sorted by priority.

### 🔴 Critical

1. **Secrets must not be committed.** `API_TOKEN`, `TELEGRAM_API_HASH`, `TELEGRAM_NOTIFY_BOT_TOKEN`, `TELEGRAM_NOTIFY_CHAT_ID` are now read from the environment (`.env` + `python-dotenv`). If any of these were ever committed in history, rotate them.
2. **Mojibake in the sources.** A significant portion of the Russian strings in `main.py` and `tg_controller.py` is stored as "broken cp1251 in UTF-8" (`"РџР°РЅРµР»СЊ"`). `text_normalizer.normalize_text` re-converts text on the fly before sending to Telegram, but the files themselves aren't fixed this way — a full re-encoding sweep is needed.
3. **Floor refreshes once every ~60 scans (≈1 min).** With `SCAN_INTERVAL_SECONDS=1` this is very slow — `discount_percent` is computed against a stale floor, producing both false and missed signals. A reduction to ~12 seconds is planned (see the `sniper-reliability-improvements` spec).
4. **Scanner only reads the latest 100 listings.** If more new lots appear in one scan (or the network lags), a below-floor lot could fall out of the window. An extra pass with `ordering="Price", lowToHigh=True` over hot collections would close the gap.
5. **Trade-lock is validated only from global-feed data.** Not all fields (`nextResaleDate`, etc.) are guaranteed to be returned in `/gifts/saling`. In rare cases this could buy a lot that can't be resold a minute later.

### 🟡 Important

6. **`winsound.Beep` blocks the event loop ~700 ms.** Each below-floor signal eats half a scan. Fix: `asyncio.to_thread(winsound.Beep, ...)` or disable it entirely on a VPS.
7. **`state.save()` on every `__setattr__`.** Miss-statistics increments rewrite the whole JSON dozens of times per second. A throttled flush or a separate stats store is needed.
8. **No supervision of background tasks.** `tg_task` and `autosell_task` are created via `asyncio.create_task` without an `add_done_callback` to restart them. If one crashes, it does so silently.
9. **Race during a 401 storm.** 7 concurrent requests could simultaneously trigger a token refresh via Telethon. An `asyncio.Lock` on `_update_token` is needed.
10. **Off-by-one in the auto-sell price-change limit.** `allowed_price_change_limit = 5 + extra_changes`, but MRKT gives **4** free. Fix planned in `sniper-reliability-improvements`.
11. **aiogram FSM is in memory.** On a bot restart, FSM states are lost and the user can get stuck "waiting for input". Persistent storage is needed.

### 🟢 Minor

12. The **"Test list for 30 TON"** button has no confirmation. An accidental tap could sell a lot below floor. A confirm showing the current floor should be added.
13. **`Scanner._scan_collection`** is no longer used (`scan_all` only uses the global feed) but remains in the code.
14. **`MIN_DISCOUNT_PERCENT=2%` vs `AUTO_BUY_MIN_DISCOUNT_CHEAP=4%`** — at a 3% discount a lot is highlighted as below-floor and beeps via winsound, but can't be bought. Visual noise.
15. **The `get_my_gifts` payload** contains duplicate keys (`lowToHigh`, `luckyBuy` appear twice in one dict). Python takes the last one — it works, but it's ugly.

### ⚪ Deferred (future features)

- **Monochrome detector** — automatic detection of "monochrome" model/background combinations via a free vision API (Gemini Flash / Cloudflare Workers AI) with a local cache. Too slow for the hot path, but suitable for a background worker.
- **Backtest engine** — saving raw `/gifts/saling` responses to jsonl to replay new strategies offline.
- **Daily P/L summary** — a trade summary to Telegram once a day.
- **Kill-switch on cumulative loss** — pause the bot when the accumulated daily loss exceeds a threshold.

---

## Basic MRKT API example

For reference — a minimal working example of fetching a token and scanning listings, from the unofficial MRKT docs.

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

In this repository that code is heavily extended — see `api.py`, `authenticator.py`, and `scanner.py` separately.

### Main MRKT endpoints used by the bot

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/auth` | exchange `tgWebAppData` for a token |
| `GET` | `/api/v1/gifts/collections` | list of collections + `floorPriceNanoTons` |
| `POST` | `/api/v1/gifts/saling` | active listings (feed and per-collection) |
| `POST` | `/api/v1/gifts/buy` | buy by id |
| `POST` | `/api/v1/gifts` | own inventory (with the `isListed` filter) |
| `POST` | `/api/v1/gifts/sale` | list for sale |
| `POST` | `/api/v1/gifts/sale/change-price` | change a listing's price |
| `POST` | `/api/v1/gifts/sale/cancel` | cancel a sale |
| `POST` | `/api/v1/gifts/sell-by-spice` | buy +5 price changes for spice |
| `POST` | `/api/v1/orders/top` | top bid for a collection/model |
| `POST` | `/api/v1/orders` | order-book depth |
| `GET` | `/api/v1/orders/all-collection-top` | top bid across all collections |
| `POST` | `/api/v1/orders/fill/` | instant order execution |
| `POST` | `/api/v1/orders/get-my-orders` | own orders |

---

## License

Use at your own risk. The bot operates with a real TON balance — any bug in the buy/sell logic can lead to loss of funds. Test changes on a small balance.
