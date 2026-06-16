"""
MRKT NFT Gift Sniper Bot - Main Entry Point
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import config
from api import MRKTClient
from autosell import AutoSellEngine
from autosell_store import AutoSellStore
from bot import (
    play_alert_sound,
    print_banner,
    print_error,
    print_floor_prices,
    print_listings,
    print_scan_complete,
    print_scan_start,
    print_status,
    print_waiting,
)
from config import (
    AUTO_BUY_ENABLED,
    AUTO_BUY_TRADE_BAN_DISCOUNT_REQUIRED_FROM_DAYS,
    AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS,
    AUTO_BUY_TRADE_BAN_MIN_DISCOUNT_PERCENT,
    RARE_MODEL_BELOW_FLOOR_FORCE_BUY,
    PAUSE_STATUS_LOG_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
    SOUND_ALERT,
    TELEGRAM_NOTIFY_BOT_TOKEN,
    TELEGRAM_NOTIFY_CHAT_ID,
    VERBOSE,
)
from rare_model_strategy import (
    RareModelCandidate,
    RareModelSettings,
    is_rare_model_below_floor_signal,
    select_rare_model_candidates,
)
from scanner import Listing, Scanner, is_zero_rarity
from sniper_metrics import (
    build_listing_key,
    get_balance_miss_bucket,
    is_already_bought_response,
    should_count_balance_miss,
)
from state import state
from text_normalizer import normalize_text
from tg_controller import run_bot

logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="  %(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mrkt.main")

logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

STARTUP_NOTIFY_STATE_FILE = Path(__file__).resolve().with_name("startup_notify_state.json")
STARTUP_NOTIFY_COOLDOWN_SECONDS = 15 * 60


async def send_telegram_notification(message: str) -> None:
    if not TELEGRAM_NOTIFY_BOT_TOKEN or not TELEGRAM_NOTIFY_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_NOTIFY_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_NOTIFY_CHAT_ID,
        "text": normalize_text(message),
        "parse_mode": "HTML",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=5) as resp:
                if not resp.ok:
                    error_text = await resp.text()
                    logger.error(
                        "Failed to send telegram notification: %s - %s",
                        resp.status,
                        error_text,
                    )
    except Exception as exc:
        logger.error("Failed to send telegram notification: %s", exc)


def should_send_startup_notification(
    now_ts: int | None = None,
    cooldown_seconds: int = STARTUP_NOTIFY_COOLDOWN_SECONDS,
) -> bool:
    """
    Anti-spam guard for startup notifications.
    If service restarts in a loop, we notify at most once per cooldown window.
    """
    ts = int(now_ts or time.time())
    if cooldown_seconds <= 0:
        return True

    last_ts = 0
    try:
        if STARTUP_NOTIFY_STATE_FILE.exists():
            data = json.loads(STARTUP_NOTIFY_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last_ts = int(data.get("last_startup_notify_ts") or 0)
    except Exception:
        last_ts = 0

    if last_ts > 0 and (ts - last_ts) < cooldown_seconds:
        return False

    try:
        STARTUP_NOTIFY_STATE_FILE.write_text(
            json.dumps({"last_startup_notify_ts": ts}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to persist startup notify state: %s", exc)
    return True


def build_success_message(listing: Listing, pricing_line: str, buy_reason: str) -> str:
    return (
        "рџЋ‰ <b>РЈРЎРџР•РЁРќРђРЇ РџРћРљРЈРџРљРђ!</b>\n\n"
        f"рџ“¦ <b>РџРѕРґР°СЂРѕРє:</b> {html.escape(listing.collection_title)}\n"
        f"рџ’° <b>РљСѓРїР»РµРЅРѕ Р·Р°:</b> {listing.listing_price_ton:.4f} TON\n"
        f"{pricing_line}"
        f"рџ§  <b>РџСЂРёС‡РёРЅР° РїРѕРєСѓРїРєРё:</b> {html.escape(buy_reason)}\n"
        f"рџ†” <code>{html.escape(listing.gift_id)}</code>\n\n"
        "вЏё <i>Р‘РѕС‚ РїРµСЂРµРІРµРґРµРЅ РІ СЂРµР¶РёРј РїР°СѓР·С‹. РќР°Р¶РјРёС‚Рµ 'РЎС‚Р°СЂС‚' РІ РјРµРЅСЋ, С‡С‚РѕР±С‹ РїСЂРѕРґРѕР»Р¶РёС‚СЊ.</i>"
    )


def is_success_buy_response(res: object) -> bool:
    if isinstance(res, list):
        return len(res) > 0
    if isinstance(res, dict):
        return not (res.get("errors") or res.get("error"))
    return False


def get_required_regular_discount(price_ton: float) -> float:
    # Keep legacy tiers and add new explicit tiers.
    if price_ton < state.threshold:
        return state.discount_cheap  # <4 by default
    if price_ton >= 20:
        return state.discount_20_plus
    if price_ton >= 10:
        return state.discount_10_20
    return state.discount_expensive  # >=4 legacy button, effectively 4-10


def get_regular_bucket_label(price_ton: float) -> str:
    if price_ton < state.threshold:
        return f"<{state.threshold:g}T"
    if price_ton >= 20:
        return "20T+"
    if price_ton >= 10:
        return "10-20T"
    return f">={state.threshold:g}T (legacy)"


async def should_skip_floor_buy_by_depth_guard(
    *,
    client: MRKTClient,
    listing: Listing,
    all_new: list[Listing],
    snapshot_cache: dict[str, list[int]],
) -> tuple[bool, str]:
    """
    Anti-dump guard for regular floor strategy.
    Fast path:
    1) check current global feed density (same collection and price region),
    2) optionally verify with one cached cheapest-snapshot request per collection.
    """
    if not bool(getattr(state, "buy_depth_guard_enabled", False)):
        return False, ""

    max_at_or_below = max(1, int(getattr(state, "buy_depth_guard_max_at_or_below", 3)))
    near_pct = max(0.0, float(getattr(state, "buy_depth_guard_near_price_percent", 0.35)))
    scan_count = max(10, min(100, int(getattr(state, "buy_depth_guard_scan_count", 30))))

    same_price_feed = 0
    at_or_below_feed = 0
    for x in all_new:
        if x.collection_name != listing.collection_name:
            continue
        if x.listing_price_nano == listing.listing_price_nano:
            same_price_feed += 1
        if x.listing_price_nano <= listing.listing_price_nano:
            at_or_below_feed += 1

    if same_price_feed > max_at_or_below:
        return (
            True,
            f"same-price crowd in feed: {same_price_feed} > {max_at_or_below}",
        )
    if at_or_below_feed > max_at_or_below:
        return (
            True,
            f"at-or-below crowd in feed: {at_or_below_feed} > {max_at_or_below}",
        )

    # Keep speed: hit extra API only when feed already hints crowd risk.
    if max(same_price_feed, at_or_below_feed) < 2:
        return False, ""

    prices = snapshot_cache.get(listing.collection_name)
    if prices is None:
        try:
            snapshot = await client.get_listings(
                collection_names=[listing.collection_name],
                low_to_high=True,
                count=scan_count,
                ordering="Price",
            )
            gifts = snapshot.get("gifts", []) if isinstance(snapshot, dict) else []
            parsed_prices: list[int] = []
            for gift in gifts:
                raw_price = gift.get("salePrice")
                if raw_price is None:
                    continue
                try:
                    parsed_prices.append(int(raw_price))
                except (TypeError, ValueError):
                    continue
            prices = sorted(parsed_prices)
            snapshot_cache[listing.collection_name] = prices
        except Exception as exc:
            # Do not block buys if verification API is temporarily failing.
            logger.warning(
                "Depth-guard snapshot failed for %s: %s",
                listing.collection_name,
                exc,
            )
            return False, ""

    if not prices:
        return False, ""

    at_or_below_live = sum(1 for p in prices if p <= listing.listing_price_nano)
    if at_or_below_live > max_at_or_below:
        return (
            True,
            f"live at-or-below crowd: {at_or_below_live} > {max_at_or_below}",
        )

    near_upper = int(round(listing.listing_price_nano * (1.0 + near_pct / 100.0)))
    near_count = sum(1 for p in prices if p <= near_upper)
    if near_count > (max_at_or_below + 2):
        return (
            True,
            f"too many near price (<= +{near_pct:.2f}%): {near_count}",
        )

    cheapest = prices[0]
    if cheapest < listing.listing_price_nano and listing.listing_price_nano > 0:
        drop_pct = ((listing.listing_price_nano - cheapest) / listing.listing_price_nano) * 100.0
        if drop_pct >= near_pct:
            return (
                True,
                f"cheapest already lower by {drop_pct:.2f}%",
            )

    return False, ""


def build_rare_settings() -> RareModelSettings:
    # User requirement: hard minimum is always 5 TON in rare mode.
    min_listing_price = max(5.0, state.rare_min_listing_price)
    threshold_price = max(5.0, state.rare_threshold)
    return RareModelSettings(
        enabled=state.rare_enabled,
        max_rarity_percent=state.rare_max_rarity_percent,
        min_listing_price_ton=min_listing_price,
        threshold_price_ton=threshold_price,
        max_premium_5_10_percent=state.rare_premium_5_10,
        max_premium_10_plus_percent=state.rare_premium_10_plus,
        below_floor_force_buy=RARE_MODEL_BELOW_FLOOR_FORCE_BUY,
    )


def effective_cadence(user_value: float) -> float:
    """Clamp user-supplied floor refresh cadence to the hard minimum."""
    return max(float(config.FLOOR_REFRESH_MIN_SECONDS), float(user_value))


async def main() -> None:
    print_banner()
    print_status("Р—Р°РїСѓСЃРє MRKT NFT Sniper Bot...")
    print_status(f"РРЅС‚РµСЂРІР°Р» СЃРєР°РЅРёСЂРѕРІР°РЅРёСЏ: {SCAN_INTERVAL_SECONDS}s")
    print()

    tg_task: asyncio.Task | None = None
    autosell_task: asyncio.Task | None = None
    startup_message = (
        "🚀 <b>MRKT NFT Sniper Bot запущен!</b>\n"
        "Для управления используйте /menu.\n"
        "Отдельный режим редких моделей: /rare (по умолчанию OFF после рестарта).\n"
        "Отдельный Auto-Sell модуль: /sell (по умолчанию OFF после рестарта)."
    )
    if TELEGRAM_NOTIFY_BOT_TOKEN:
        tg_task = asyncio.create_task(run_bot(), name="mrkt-tg-controller")


    client = MRKTClient()
    scanner = Scanner(client)
    sell_store = AutoSellStore()
    sell_engine = AutoSellEngine(client, scanner, sell_store)
    autosell_task = asyncio.create_task(sell_engine.run(), name="mrkt-autosell-worker")
    max_buy_attempts_per_scan = 3
    counted_balance_miss_keys: set[tuple[str, int]] = set()
    counted_bought_before_keys: set[tuple[str, int]] = set()
    pause_log_last_ts = 0.0

    try:
        print_status("Р—Р°РіСЂСѓР·РєР° РєРѕР»Р»РµРєС†РёР№ Рё С†РµРЅ С„Р»РѕСЂР°...")
        floor_prices = await scanner.refresh_floor_prices()
        while not floor_prices:
            print_error("РљРѕР»Р»РµРєС†РёРё РЅРµ Р·Р°РіСЂСѓР¶РµРЅС‹. РџРѕРІС‚РѕСЂ С‡РµСЂРµР· 10 СЃРµРєСѓРЅРґ...")
            await asyncio.sleep(10)
            floor_prices = await scanner.refresh_floor_prices()

        last_floor_refresh_ts = time.monotonic()

        if should_send_startup_notification():
            await send_telegram_notification(startup_message)
        else:
            logger.info(
                "Startup notification suppressed by cooldown (%ss).",
                STARTUP_NOTIFY_COOLDOWN_SECONDS,
            )

        print_floor_prices(floor_prices, scanner._collection_titles)

        if not state.is_running:
            print_status("вЏё Р‘РѕС‚ Р·Р°РїСѓС‰РµРЅ РІ РїР°СѓР·Рµ (СЃРѕСЃС‚РѕСЏРЅРёРµ РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРѕ СЃ РґРёСЃРєР°).")

        while True:
            now_ts = time.monotonic()
            cadence = effective_cadence(state.floor_refresh_seconds)
            if state.is_running and (now_ts - last_floor_refresh_ts) >= cadence:
                print_status("РћР±РЅРѕРІР»РµРЅРёРµ С†РµРЅ С„Р»РѕСЂР°...")
                try:
                    await scanner.refresh_floor_prices()
                    scanner.clear_seen()
                except Exception as exc:
                    logger.warning(
                        "Floor refresh failed, keeping previous floors: %s", exc
                    )
                # Update ts even on failure so we do not hammer the endpoint.
                last_floor_refresh_ts = time.monotonic()

            if not state.is_running:
                now_ts = time.monotonic()
                interval = max(1.0, float(PAUSE_STATUS_LOG_INTERVAL_SECONDS))
                if now_ts - pause_log_last_ts >= interval:
                    print_status("⏸ Бот на паузе (через ТГ меню). Нажмите 'Статус' в /menu для запуска.")
                    pause_log_last_ts = now_ts
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                continue

            print_scan_start(len(scanner._floor_prices))
            start = time.monotonic()
            try:
                below_floor, all_new = await scanner.scan_all()
            except Exception as exc:
                print_error(f"РћС€РёР±РєР° СЃРєР°РЅРёСЂРѕРІР°РЅРёСЏ: {exc}")
                logger.exception("Scan failed")
                below_floor, all_new = [], []
            elapsed = time.monotonic() - start
            print_scan_complete(len(all_new), len(below_floor), elapsed)

            if all_new:
                print_listings(below_floor, all_new)

            rare_settings = build_rare_settings()
            rare_model_candidates: list[RareModelCandidate] = []
            rare_model_below_floor: list[Listing] = []
            if AUTO_BUY_ENABLED and rare_settings.enabled and all_new:
                rare_model_candidates = select_rare_model_candidates(
                    all_new,
                    settings=rare_settings,
                    excluded_gift_ids={listing.gift_id for listing in below_floor},
                )
                rare_model_below_floor = [
                    listing
                    for listing in below_floor
                    if is_rare_model_below_floor_signal(listing, rare_settings)
                ]

                if rare_model_candidates:
                    best = rare_model_candidates[0]
                    print_status(
                        f"рџ§  Rare candidates: {len(rare_model_candidates)} "
                        f"(best rarity {best.rarity_percent:.2f}%, premium +{best.premium_percent:.2f}%)"
                    )
                if rare_model_below_floor:
                    print_status(
                        f"рџ”Ґ Rare below-floor forced-buy signals: {len(rare_model_below_floor)}"
                    )

            if SOUND_ALERT and (below_floor or rare_model_candidates or rare_model_below_floor):
                play_alert_sound()

            if AUTO_BUY_ENABLED and (below_floor or rare_model_candidates or rare_model_below_floor):
                buy_targets: list[tuple[str, Listing, RareModelCandidate | None]] = []
                depth_guard_snapshot_cache: dict[str, list[int]] = {}

                rare_floor_ids = {x.gift_id for x in rare_model_below_floor}
                for listing in rare_model_below_floor:
                    buy_targets.append(("rare_model_floor", listing, None))

                for listing in below_floor:
                    if listing.gift_id in rare_floor_ids:
                        continue
                    buy_targets.append(("floor", listing, None))

                for candidate in rare_model_candidates:
                    buy_targets.append(("rare_model", candidate.listing, candidate))

                buy_attempts_in_scan = 0
                for mode, listing, rare_candidate in buy_targets:
                    if listing.trade_lock_blocked:
                        lock_days = max(0.0, listing.trade_lock_seconds / 86400.0)
                        unlock_text = "unknown"
                        if listing.trade_lock_until_ts:
                            unlock_text = datetime.fromtimestamp(
                                listing.trade_lock_until_ts,
                                tz=timezone.utc,
                            ).strftime("%Y-%m-%d %H:%M UTC")
                        if lock_days >= AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS:
                            ban_reason = (
                                f"lock {lock_days:.1f}d >= "
                                f"{AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS:g}d"
                            )
                        else:
                            ban_reason = (
                                f"lock {lock_days:.1f}d and discount "
                                f"{listing.discount_percent:.2f}% < "
                                f"{AUTO_BUY_TRADE_BAN_MIN_DISCOUNT_PERCENT:.2f}% "
                                f"for {AUTO_BUY_TRADE_BAN_DISCOUNT_REQUIRED_FROM_DAYS:g}-"
                                f"{AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS:g}d window"
                            )
                        logger.warning(
                            "Skipping %s auto-buy for %s: trade-lock (%s), source=%s, unlock=%s",
                            mode,
                            listing.collection_title,
                            ban_reason,
                            listing.trade_lock_source or "unknown",
                            unlock_text,
                        )
                        print_status(
                            "SKIP trade-lock: "
                            f"{listing.collection_title} ({ban_reason}, unlock: {unlock_text})"
                        )
                        continue

                    # Separate balances for normal and rare strategy.
                    balance_limit = (
                        state.rare_balance if mode.startswith("rare_model") else state.balance
                    )
                    if listing.listing_price_ton > balance_limit:
                        miss_key = build_listing_key(
                            listing.gift_id,
                            listing.listing_price_nano,
                        )
                        if (
                            should_count_balance_miss(
                                price_ton=listing.listing_price_ton,
                                discount_percent=listing.discount_percent,
                                balance_limit_ton=balance_limit,
                            )
                            and miss_key not in counted_balance_miss_keys
                        ):
                            counted_balance_miss_keys.add(miss_key)
                            state.stat_missed_balance_high_value = (
                                int(state.stat_missed_balance_high_value) + 1
                            )
                            bucket = get_balance_miss_bucket(listing.listing_price_ton)
                            if bucket == "8_20":
                                state.stat_missed_balance_8_20 = (
                                    int(state.stat_missed_balance_8_20) + 1
                                )
                            elif bucket == "20_50":
                                state.stat_missed_balance_20_50 = (
                                    int(state.stat_missed_balance_20_50) + 1
                                )
                            else:
                                state.stat_missed_balance_50_plus = (
                                    int(state.stat_missed_balance_50_plus) + 1
                                )

                            today_key = datetime.now(timezone.utc).date().isoformat()
                            if state.stat_missed_balance_best_day_date != today_key:
                                state.stat_missed_balance_best_day_date = today_key
                                state.stat_missed_balance_best_day_discount = 0.0
                                state.stat_missed_balance_best_day_price = 0.0
                                state.stat_missed_balance_best_day_floor = 0.0
                                state.stat_missed_balance_best_day_collection = ""
                                state.stat_missed_balance_best_day_gift_id = ""

                            if (
                                float(listing.discount_percent)
                                > float(state.stat_missed_balance_best_day_discount)
                            ):
                                state.stat_missed_balance_best_day_date = today_key
                                state.stat_missed_balance_best_day_discount = float(
                                    listing.discount_percent
                                )
                                state.stat_missed_balance_best_day_price = float(
                                    listing.listing_price_ton
                                )
                                state.stat_missed_balance_best_day_floor = float(
                                    listing.floor_price_ton
                                )
                                state.stat_missed_balance_best_day_collection = (
                                    listing.collection_title
                                )
                                state.stat_missed_balance_best_day_gift_id = (
                                    listing.gift_id
                                )
                        logger.warning(
                            "Skipping %s auto-buy (price %.4f > %.4f TON balance)",
                            mode,
                            listing.listing_price_ton,
                            balance_limit,
                        )
                        continue

                    if mode == "floor":
                        if (
                            bool(getattr(state, "zero_rarity_gate_enabled", True))
                            and is_zero_rarity(listing)
                        ):
                            required_zero = float(state.zero_rarity_min_discount)
                            if listing.discount_percent < required_zero:
                                logger.warning(
                                    "SKIP zero-rarity: required %.2f%%, got %.2f%% (%s)",
                                    required_zero,
                                    listing.discount_percent,
                                    listing.collection_title,
                                )
                                continue

                        required_discount = get_required_regular_discount(
                            listing.listing_price_ton
                        )
                        if listing.discount_percent < required_discount:
                            logger.warning(
                                "Skipping floor auto-buy (%s): discount %.2f%% < %.2f%%",
                                get_regular_bucket_label(listing.listing_price_ton),
                                listing.discount_percent,
                                required_discount,
                            )
                            continue

                        skip_depth, depth_reason = await should_skip_floor_buy_by_depth_guard(
                            client=client,
                            listing=listing,
                            all_new=all_new,
                            snapshot_cache=depth_guard_snapshot_cache,
                        )
                        if skip_depth:
                            logger.warning(
                                "Skipping floor auto-buy for %s due to anti-dump: %s",
                                listing.collection_title,
                                depth_reason,
                            )
                            print_status(
                                f"SKIP anti-dump: {listing.collection_title} ({depth_reason})"
                            )
                            continue

                        buy_reason = (
                            f"РћР±С‹С‡РЅР°СЏ СЃС‚СЂР°С‚РµРіРёСЏ: СЃРєРёРґРєР° {listing.discount_percent:.2f}% "
                            f"РІ РєРѕСЂР·РёРЅРµ {get_regular_bucket_label(listing.listing_price_ton)}."
                        )
                        pricing_line = (
                            f"рџ“‰ <b>РЎРєРёРґРєР°:</b> {listing.discount_percent:.2f}% "
                            f"(Р¤Р»РѕСЂ: {listing.floor_price_ton:.4f} TON)\n"
                        )
                        print_status(
                            f"рџ›’ BUY floor: {listing.collection_title} {listing.listing_price_ton:.4f} TON "
                            f"(discount {listing.discount_percent:.2f}%)"
                        )
                    elif mode == "rare_model_floor":
                        rarity = listing.model_rarity_percent or 0.0
                        buy_reason = (
                            f"Rare-model forced-buy: СЂРµРґРєРѕСЃС‚СЊ {rarity:.2f}% Рё С†РµРЅР° РЅРёР¶Рµ С„Р»РѕСЂР° "
                            f"РЅР° {listing.discount_percent:.2f}%."
                        )
                        pricing_line = (
                            f"рџ“‰ <b>РЎРєРёРґРєР°:</b> {listing.discount_percent:.2f}% "
                            f"(Р¤Р»РѕСЂ: {listing.floor_price_ton:.4f} TON)\n"
                            f"рџ§¬ <b>Р РµРґРєРѕСЃС‚СЊ РјРѕРґРµР»Рё:</b> {rarity:.2f}%\n"
                        )
                        print_status(
                            f"рџ”Ґ BUY rare below-floor: {listing.collection_title} {listing.listing_price_ton:.4f} TON "
                            f"(rarity {rarity:.2f}%, discount {listing.discount_percent:.2f}%)"
                        )
                    else:
                        if rare_candidate is None:
                            continue
                        buy_reason = (
                            f"Rare-model premium: СЂРµРґРєРѕСЃС‚СЊ {rare_candidate.rarity_percent:.2f}% "
                            f"РїСЂРё РїСЂРµРјРёРё +{rare_candidate.premium_percent:.2f}% "
                            f"(Р»РёРјРёС‚ {rare_candidate.premium_limit_percent:.2f}%)."
                        )
                        pricing_line = (
                            f"рџ“€ <b>РџСЂРµРјРёСЏ Рє С„Р»РѕСЂСѓ:</b> +{rare_candidate.premium_percent:.2f}% "
                            f"(Р»РёРјРёС‚ {rare_candidate.premium_limit_percent:.2f}%)\n"
                            f"рџ§¬ <b>Р РµРґРєРѕСЃС‚СЊ РјРѕРґРµР»Рё:</b> {rare_candidate.rarity_percent:.2f}%\n"
                        )
                        print_status(
                            f"рџ§  BUY rare premium: {listing.collection_title} {listing.listing_price_ton:.4f} TON "
                            f"(rarity {rare_candidate.rarity_percent:.2f}%, premium +{rare_candidate.premium_percent:.2f}%)"
                        )

                    res = await client.buy_gift(listing.gift_id)
                    buy_attempts_in_scan += 1
                    if res is None:
                        print_error("РћС€РёР±РєР° РїРѕРєСѓРїРєРё: Р·Р°РїСЂРѕСЃ РЅРµ СѓРґР°Р»СЃСЏ.")
                    elif isinstance(res, list) and len(res) == 0:
                        miss_key = build_listing_key(
                            listing.gift_id,
                            listing.listing_price_nano,
                        )
                        if miss_key not in counted_bought_before_keys:
                            counted_bought_before_keys.add(miss_key)
                            state.stat_missed_bought_before = (
                                int(state.stat_missed_bought_before) + 1
                            )
                        print_error("РџРѕРґР°СЂРѕРє СѓР¶Рµ РїСЂРѕРґР°РЅ (РѕС‚РІРµС‚: []).")
                    elif isinstance(res, dict) and (res.get("errors") or res.get("error")):
                        if is_already_bought_response(res):
                            miss_key = build_listing_key(
                                listing.gift_id,
                                listing.listing_price_nano,
                            )
                            if miss_key not in counted_bought_before_keys:
                                counted_bought_before_keys.add(miss_key)
                                state.stat_missed_bought_before = (
                                    int(state.stat_missed_bought_before) + 1
                                )
                        print_error(f"РћС€РёР±РєР° СЃРµСЂРІРµСЂР°: {res}")
                    elif is_success_buy_response(res):
                        print_status(f"рџЋ‰ РљРЈРџР›Р•РќРћ! РћС‚РІРµС‚: {res}")
                        sell_store.record_purchase(
                            gift_id=listing.gift_id,
                            buy_price_ton=listing.listing_price_ton,
                            collection_name=listing.collection_name,
                            collection_title=listing.collection_title,
                            model_name=listing.model_name or "",
                            model_rarity_percent=listing.model_rarity_percent,
                        )
                        logger.info(
                            "Recorded purchase for Auto-Sell: %s at %.4f TON",
                            listing.gift_id,
                            listing.listing_price_ton,
                        )
                        print_status("рџЋЇ РЈСЃРїРµС€РЅР°СЏ РїРѕРєСѓРїРєР°, Р±РѕС‚ РїРµСЂРµС…РѕРґРёС‚ РІ РїР°СѓР·Сѓ.")
                        await send_telegram_notification(
                            build_success_message(
                                listing=listing,
                                pricing_line=pricing_line,
                                buy_reason=buy_reason,
                            )
                        )
                        state.is_running = False
                        break
                    else:
                        print_status(f"РќРµРѕР¶РёРґР°РЅРЅС‹Р№ РѕС‚РІРµС‚: {res}")

                    # Limit attempts per scan to avoid API spam while still
                    # trying next candidates when first one is already sold.
                    if buy_attempts_in_scan >= max_buy_attempts_per_scan:
                        break

            sleep_seconds = max(0.0, float(SCAN_INTERVAL_SECONDS))
            if sleep_seconds > 0:
                print_waiting(math.ceil(sleep_seconds))
                await asyncio.sleep(sleep_seconds)
                print(" " * 60, end="\r")

    except asyncio.CancelledError:
        print_status("Р‘РѕС‚ РѕСЃС‚Р°РЅРѕРІР»РµРЅ.")
    except KeyboardInterrupt:
        print_status("Р‘РѕС‚ РѕСЃС‚Р°РЅРѕРІР»РµРЅ РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј.")
    except Exception as exc:
        print_error(f"РљСЂРёС‚РёС‡РµСЃРєР°СЏ РѕС€РёР±РєР°: {exc}")
        logger.exception("Fatal error in main()")
    finally:
        await client.close()
        if tg_task:
            tg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tg_task
        if autosell_task:
            autosell_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await autosell_task
        print_status("Р”Рѕ РІСЃС‚СЂРµС‡Рё! рџ‘‹")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

