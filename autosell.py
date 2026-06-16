"""
Separate Auto-Sell engine.
Runs independently from scan/buy flow.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

import config
from api import MRKTClient
from autosell_store import AutoSellStore
from scanner import Scanner
from state import state

logger = logging.getLogger("mrkt.autosell")

AUTO_STATUSES_ACTIVE = ("NEW", "LISTED", "WAIT_PROMPT")
AUTO_STATUSES_TRACKED = ("NEW", "LISTED", "WAIT_PROMPT", "HOLD")
AUTO_SELL_MIN_MARKET_PRICE_TON = 0.01
AUTO_SELL_SALE_RETRY_COOLDOWN_SECONDS = 30
MISSING_INVENTORY_CONFIRM_CYCLES = 4


def _now_ts() -> int:
    return int(time.time())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _any_price_to_ton(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    # Most MRKT API prices are in nanoTON.
    if parsed > 1_000_000:
        return parsed / 1_000_000_000.0
    return parsed


def compute_stop_loss_price_ton(buy_price_ton: float, loss_cap_percent: float) -> float:
    return float(buy_price_ton) * (1.0 - float(loss_cap_percent) / 100.0)


def is_rare_model_protected(
    model_rarity_percent: float | None,
    rare_protect_percent: float,
) -> bool:
    if model_rarity_percent is None:
        return False
    try:
        return float(model_rarity_percent) <= float(rare_protect_percent)
    except (TypeError, ValueError):
        return False


def should_trigger_stuck_prompt(
    *,
    age_seconds: int,
    current_price_ton: float,
    prompt_price_threshold_ton: float,
    stuck_seconds: int,
) -> bool:
    return (
        age_seconds >= int(stuck_seconds)
        and float(current_price_ton) >= float(prompt_price_threshold_ton)
    )


def allowed_price_change_limit(extra_changes: int) -> int:
    return int(config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE) + max(0, int(extra_changes))


def _parse_best_order_payload(payload: Any) -> tuple[str | None, float | None]:
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("order"), dict):
            candidates.append(payload["order"])
        if isinstance(payload.get("orders"), list):
            candidates.extend([x for x in payload["orders"] if isinstance(x, dict)])
        if not candidates:
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend([x for x in payload if isinstance(x, dict)])

    best_price: float | None = None
    best_order_id: str | None = None
    for order in candidates:
        order_id = (
            order.get("id")
            or order.get("orderId")
            or order.get("orderID")
            or order.get("uuid")
        )
        raw_price = (
            order.get("price")
            or order.get("priceNanoTons")
            or order.get("priceNanoTONs")
            or order.get("orderPrice")
            or order.get("bidPrice")
            or order.get("salePrice")
        )
        price_ton = _any_price_to_ton(raw_price)
        if price_ton is None:
            continue
        if best_price is None or price_ton > best_price:
            best_price = price_ton
            best_order_id = str(order_id) if order_id is not None else None

    return best_order_id, best_price


@dataclass(slots=True)
class SellRuntimeSettings:
    enabled: bool
    balance_ton: float
    price_threshold_ton: float
    target_profit_5_10_percent: float
    target_profit_10_plus_percent: float
    floor_premium_5_10_percent: float
    floor_premium_10_plus_percent: float
    relist_interval_seconds: int
    stuck_seconds: int
    prompt_price_threshold_ton: float
    loss_cap_percent: float
    rare_protect_percent: float
    order_mode_critical_only: bool


def build_sell_runtime_settings() -> SellRuntimeSettings:
    return SellRuntimeSettings(
        enabled=bool(state.sell_enabled),
        balance_ton=max(0.0, float(state.sell_balance)),
        price_threshold_ton=max(5.0, float(state.sell_price_threshold)),
        target_profit_5_10_percent=max(0.0, float(state.sell_target_profit_5_10)),
        target_profit_10_plus_percent=max(0.0, float(state.sell_target_profit_10_plus)),
        floor_premium_5_10_percent=max(0.0, float(state.sell_floor_premium_5_10)),
        floor_premium_10_plus_percent=max(0.0, float(state.sell_floor_premium_10_plus)),
        relist_interval_seconds=max(30, int(state.sell_relist_interval_seconds)),
        stuck_seconds=max(300, int(state.sell_stuck_hours * 3600)),
        prompt_price_threshold_ton=max(0.0, float(state.sell_prompt_price_threshold)),
        loss_cap_percent=max(0.0, float(state.sell_loss_cap_percent)),
        rare_protect_percent=max(0.0, float(state.sell_rare_protect_percent)),
        order_mode_critical_only=bool(state.sell_order_mode_critical_only),
    )


class AutoSellEngine:
    def __init__(self, client: MRKTClient, scanner: Scanner, store: AutoSellStore) -> None:
        self.client = client
        self.scanner = scanner
        self.store = store
        self._last_floor_refresh_ts = 0
        self._last_inventory_refresh_ts = 0
        self._next_inventory_retry_ts = 0
        self._inventory: dict[str, dict[str, Any]] = {}
        self._inventory_fetch_ok = True
        self._missing_inventory_streak: dict[str, int] = {}

    async def run(self) -> None:
        logger.info("Auto-Sell worker started (independent loop).")
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                logger.info("Auto-Sell worker stopped.")
                raise
            except Exception:
                logger.exception("Auto-Sell tick failed")
            await asyncio.sleep(5)

    async def _tick(self) -> None:
        now_ts = _now_ts()
        settings = build_sell_runtime_settings()

        if not settings.enabled:
            return

        await self._handle_prompt_timeouts(now_ts)
        await self._handle_prompt_actions(settings, now_ts)
        active_lots = self.store.list_lots(list(AUTO_STATUSES_ACTIVE))

        # Keep tracking inventory, even when auto-sell mode is disabled.
        await self._refresh_inventory_if_needed(now_ts)
        self._sync_lot_status_with_inventory(now_ts)

        if now_ts - self._last_floor_refresh_ts >= 60:
            try:
                await self.scanner.refresh_floor_prices()
            except Exception:
                logger.exception("Floor refresh failed in Auto-Sell worker")
            self._last_floor_refresh_ts = now_ts

        for lot in active_lots:
            await self._process_lot(lot, settings, now_ts)

    async def _refresh_inventory_if_needed(self, now_ts: int) -> None:
        if now_ts < self._next_inventory_retry_ts:
            return
        if now_ts - self._last_inventory_refresh_ts < 20:
            return
        inventory, fetch_ok = await self._fetch_my_inventory()
        self._inventory = inventory
        self._inventory_fetch_ok = fetch_ok
        if not fetch_ok:
            logger.warning(
                "Inventory sync is partial/failed; skip SOLD transitions until API recovers."
            )
            # Reduce API/log spam while endpoint is unstable.
            self._next_inventory_retry_ts = now_ts + 120
        else:
            self._next_inventory_retry_ts = 0
        self._last_inventory_refresh_ts = now_ts

    async def _fetch_my_inventory(self) -> tuple[dict[str, dict[str, Any]], bool]:
        result: dict[str, dict[str, Any]] = {}
        fetch_ok = True
        for is_listed in (True, False):
            cursor = ""
            for _ in range(20):
                data = await self.client.get_my_gifts(
                    is_listed=is_listed,
                    cursor=cursor,
                    count=20,
                )
                if isinstance(data, dict) and data.get("_failed"):
                    fetch_ok = False
                    break
                gifts = data.get("gifts", []) if isinstance(data, dict) else []
                if not isinstance(gifts, list):
                    gifts = []
                for gift in gifts:
                    if not isinstance(gift, dict):
                        continue
                    gift_id = gift.get("id")
                    if gift_id:
                        result[str(gift_id)] = gift
                next_cursor = ""
                if isinstance(data, dict):
                    next_cursor = str(data.get("cursor") or "").strip()
                if not next_cursor:
                    break
                cursor = next_cursor
        return result, fetch_ok

    def _sync_lot_status_with_inventory(self, now_ts: int) -> None:
        lots = self.store.list_lots(list(AUTO_STATUSES_TRACKED))
        inventory_ids = set(self._inventory.keys())
        for lot in lots:
            gift_id = str(lot["gift_id"])
            status = str(lot["status"])
            gift = self._inventory.get(gift_id)

            if gift is None:
                if not self._inventory_fetch_ok:
                    # Do not treat "not found" as sold while inventory API is unstable.
                    continue

                # Inventory endpoint can be inconsistent. Avoid instant false-SOLD.
                # Require repeated absence across several sync cycles.
                if status in {"LISTED", "WAIT_PROMPT"}:
                    streak = int(self._missing_inventory_streak.get(gift_id, 0)) + 1
                    self._missing_inventory_streak[gift_id] = streak
                    if streak >= MISSING_INVENTORY_CONFIRM_CYCLES:
                        logger.info(
                            "Gift %s absent from inventory for %s cycles, move to HOLD (safe mode).",
                            gift_id,
                            streak,
                        )
                        self.store.update_lot(
                            gift_id,
                            status="HOLD",
                            last_error="missing_inventory_confirmed",
                        )
                        self.store.log_action(
                            gift_id,
                            "missing_inventory_confirmed_hold",
                            "AUTO",
                            {"streak": streak},
                        )
                continue
            else:
                self._missing_inventory_streak.pop(gift_id, None)

            is_on_sale = bool(gift.get("isOnSale"))
            sale_price_ton = _any_price_to_ton(gift.get("salePrice"))
            if is_on_sale:
                self.store.update_lot(
                    gift_id,
                    status="LISTED" if status != "WAIT_PROMPT" else "WAIT_PROMPT",
                    listed_price=sale_price_ton,
                    last_action_at=lot.get("last_action_at") or now_ts,
                )
            elif status == "LISTED":
                # Listing is gone but gift is still ours: prepare re-listing.
                self.store.update_lot(gift_id, status="NEW")

            # Keep model rarity synced when available.
            rarity_percent = _any_price_to_ton(gift.get("modelRarityPerMille"))
            if rarity_percent is not None:
                self.store.update_lot(
                    gift_id,
                    model_rarity_percent=rarity_percent / 10.0,
                )

            if gift_id in inventory_ids:
                inventory_ids.remove(gift_id)

    async def _process_lot(
        self,
        lot: dict[str, Any],
        settings: SellRuntimeSettings,
        now_ts: int,
    ) -> None:
        gift_id = str(lot["gift_id"])
        status = str(lot.get("status") or "")
        gift = self._inventory.get(gift_id)

        # Allow NEW lots to be listed even if inventory endpoint is unstable.
        if gift is None and status != "NEW":
            return

        if status == "WAIT_PROMPT":
            return

        if self._is_rare_protected(lot, settings):
            if status != "HOLD":
                self.store.update_lot(gift_id, status="HOLD", rare_flag=1)
                self.store.log_action(
                    gift_id,
                    "rare_protected_hold",
                    "AUTO",
                    {"rarity_percent": lot.get("model_rarity_percent")},
                )
            return

        if _safe_float(lot.get("buy_price")) > settings.balance_ton > 0:
            return

        purchase = self.store.get_purchase(gift_id)
        bought_at_ts = int((purchase or {}).get("bought_at") or 0)
        if bought_at_ts and now_ts < bought_at_ts + 60:
            # MRKT cooldown after buy: listing is available after ~60s.
            return

        floor_ton = self._get_collection_floor_ton(str(lot.get("collection_name") or ""))
        listed_price_ton = (
            _any_price_to_ton(gift.get("salePrice")) if gift else None
        ) or _safe_float(lot.get("listed_price"))
        first_listed_at = int(lot.get("first_listed_at") or 0)
        next_critical_at = int(lot.get("next_critical_at") or 0)
        if next_critical_at and now_ts < next_critical_at:
            return

        desired_price = self._compute_desired_price_ton(lot, floor_ton, settings)
        min_price = self._compute_stop_loss_price_ton(lot, settings)
        is_on_sale = bool(gift.get("isOnSale")) if gift else False

        if not is_on_sale:
            last_action_at = int(lot.get("last_action_at") or 0)
            if (
                str(lot.get("last_error") or "") == "sale_failed"
                and now_ts - last_action_at < AUTO_SELL_SALE_RETRY_COOLDOWN_SECONDS
            ):
                return
            ok = await self._set_initial_listing(gift_id, desired_price, lot, now_ts)
            if not ok:
                return
            listed_price_ton = desired_price
            first_listed_at = now_ts
        elif status == "NEW":
            # Gift is already listed on market; switch local lot to LISTED
            # instead of sending duplicate /sale requests.
            listed_from_inventory = _any_price_to_ton(gift.get("salePrice"))
            if listed_from_inventory is not None:
                listed_price_ton = listed_from_inventory
            elif listed_price_ton <= 0:
                listed_price_ton = desired_price
            self.store.update_lot(
                gift_id,
                status="LISTED",
                listed_price=listed_price_ton,
                first_listed_at=first_listed_at or now_ts,
                last_error=None,
            )
            if not first_listed_at:
                first_listed_at = now_ts

        if not first_listed_at:
            first_listed_at = now_ts
            self.store.update_lot(gift_id, first_listed_at=now_ts)

        await self._maybe_relist(
            lot=lot,
            listed_price_ton=listed_price_ton,
            desired_price_ton=desired_price,
            min_price_ton=min_price,
            settings=settings,
            now_ts=now_ts,
        )

        # 2h no-profit critical branch for lots >= 4 TON
        age_seconds = now_ts - int(first_listed_at)
        current_price = _safe_float(listed_price_ton, _safe_float(lot.get("buy_price")))
        if (
            should_trigger_stuck_prompt(
                age_seconds=age_seconds,
                current_price_ton=current_price,
                prompt_price_threshold_ton=settings.prompt_price_threshold_ton,
                stuck_seconds=settings.stuck_seconds,
            )
            and str(lot.get("status")) in {"LISTED", "NEW"}
        ):
            open_prompt = self.store.get_open_prompt_for_gift(gift_id)
            if not open_prompt:
                await self._create_stuck_prompt(lot, floor_ton, settings, now_ts)

    def _is_rare_protected(
        self,
        lot: dict[str, Any],
        settings: SellRuntimeSettings,
    ) -> bool:
        rarity = lot.get("model_rarity_percent")
        return is_rare_model_protected(rarity, settings.rare_protect_percent)

    def _get_collection_floor_ton(self, collection_name: str) -> float:
        floor_nano = self.scanner._floor_prices.get(collection_name, 0)
        if floor_nano:
            return float(floor_nano) / 1_000_000_000.0
        return 0.0

    def _compute_desired_price_ton(
        self,
        lot: dict[str, Any],
        floor_ton: float,
        settings: SellRuntimeSettings,
    ) -> float:
        buy_price = _safe_float(lot.get("buy_price"))
        if buy_price >= settings.price_threshold_ton:
            target_profit = settings.target_profit_10_plus_percent
            floor_premium = settings.floor_premium_10_plus_percent
        else:
            target_profit = settings.target_profit_5_10_percent
            floor_premium = settings.floor_premium_5_10_percent

        buy_target = buy_price * (1.0 + target_profit / 100.0)
        floor_target = (
            floor_ton * (1.0 + floor_premium / 100.0)
            if floor_ton > 0
            else buy_target
        )
        desired = max(buy_target, floor_target)
        return max(
            desired,
            self._compute_stop_loss_price_ton(lot, settings),
            AUTO_SELL_MIN_MARKET_PRICE_TON,
        )

    def _compute_stop_loss_price_ton(
        self,
        lot: dict[str, Any],
        settings: SellRuntimeSettings,
    ) -> float:
        buy_price = _safe_float(lot.get("buy_price"))
        return compute_stop_loss_price_ton(buy_price, settings.loss_cap_percent)

    async def _set_initial_listing(
        self,
        gift_id: str,
        price_ton: float,
        lot: dict[str, Any],
        now_ts: int,
    ) -> bool:
        result = await self.client.sell_gifts([gift_id], price_ton)
        if not result:
            self.store.update_lot(
                gift_id,
                status="HOLD",
                last_error="sale_failed",
                last_action_at=now_ts,
            )
            self.store.log_action(
                gift_id,
                "initial_sale_failed",
                "AUTO",
                {"price_ton": price_ton, "status_after": "HOLD"},
            )
            return False

        self.store.update_lot(
            gift_id,
            status="LISTED",
            listed_price=price_ton,
            first_listed_at=lot.get("first_listed_at") or now_ts,
            last_action_at=now_ts,
            last_error=None,
        )
        self.store.log_action(
            gift_id,
            "initial_sale_set",
            "AUTO",
            {"price_ton": price_ton},
        )
        return True

    async def _maybe_relist(
        self,
        *,
        lot: dict[str, Any],
        listed_price_ton: float,
        desired_price_ton: float,
        min_price_ton: float,
        settings: SellRuntimeSettings,
        now_ts: int,
    ) -> None:
        gift_id = str(lot["gift_id"])
        last_action_at = int(lot.get("last_action_at") or 0)
        if now_ts - last_action_at < settings.relist_interval_seconds:
            return

        relist_count = int(lot.get("relist_count") or 0)
        allowed_changes = allowed_price_change_limit(int(lot.get("extra_changes") or 0))
        if relist_count >= allowed_changes:
            open_prompt = self.store.get_open_prompt_for_gift(gift_id)
            if not open_prompt:
                await self._create_limit_prompt(lot, now_ts)
            return

        target_price = max(desired_price_ton, min_price_ton, AUTO_SELL_MIN_MARKET_PRICE_TON)
        if abs(target_price - listed_price_ton) < 1e-9:
            return

        result = await self.client.change_sale_price([gift_id], target_price)
        if not result:
            self.store.update_lot(gift_id, last_error="change_price_failed")
            self.store.log_action(
                gift_id,
                "change_price_failed",
                "AUTO",
                {
                    "from": listed_price_ton,
                    "to": target_price,
                    "relist_count": relist_count,
                },
            )
            return

        self.store.update_lot(
            gift_id,
            listed_price=target_price,
            relist_count=relist_count + 1,
            last_action_at=now_ts,
            status="LISTED",
            last_error=None,
        )
        self.store.log_action(
            gift_id,
            "change_price",
            "AUTO",
            {
                "from": listed_price_ton,
                "to": target_price,
                "relist_count": relist_count + 1,
                "allowed_changes": allowed_changes,
            },
        )

    async def _create_limit_prompt(self, lot: dict[str, Any], now_ts: int) -> None:
        gift_id = str(lot["gift_id"])
        deadline = now_ts + config.AUTO_SELL_CRITICAL_PROMPT_TIMEOUT_SECONDS
        options = ["hold", "sell_now", "buy5"]
        prompt_id = self.store.create_critical_prompt(
            gift_id=gift_id,
            reason="relist_limit_reached",
            deadline_ts=deadline,
            options=options,
            default_on_timeout="hold",
        )
        self.store.update_lot(gift_id, status="WAIT_PROMPT")
        allowed_changes = int(config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE) + int(
            lot.get("extra_changes") or 0
        )
        self.store.log_action(
            gift_id,
            "critical_prompt_created",
            "AUTO",
            {
                "prompt_id": prompt_id,
                "reason": "relist_limit_reached",
                "allowed_changes": allowed_changes,
            },
        )
        await self._send_critical_prompt_message(
            prompt_id,
            lot,
            f"Достигнут лимит {config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE} изменений цены",
        )

    async def _create_stuck_prompt(
        self,
        lot: dict[str, Any],
        floor_ton: float,
        settings: SellRuntimeSettings,
        now_ts: int,
    ) -> None:
        gift_id = str(lot["gift_id"])
        deadline = now_ts + config.AUTO_SELL_CRITICAL_PROMPT_TIMEOUT_SECONDS
        options = ["hold", "sell_now", "buy5", "extend_30", "extend_60", "extend_120", "extend_360", "custom"]
        prompt_id = self.store.create_critical_prompt(
            gift_id=gift_id,
            reason="stuck_2h_no_profit",
            deadline_ts=deadline,
            options=options,
            default_on_timeout="hold",
        )
        self.store.update_lot(
            gift_id,
            status="WAIT_PROMPT",
            last_floor=floor_ton,
            next_critical_at=now_ts + settings.stuck_seconds,
        )
        self.store.log_action(
            gift_id,
            "critical_prompt_created",
            "AUTO",
            {"prompt_id": prompt_id, "reason": "stuck_2h_no_profit"},
        )
        await self._send_critical_prompt_message(prompt_id, lot, "Лот не продался в плюс за 2 часа")

    async def _send_critical_prompt_message(
        self,
        prompt_id: int,
        lot: dict[str, Any],
        reason_text: str,
    ) -> None:
        if not config.TELEGRAM_NOTIFY_BOT_TOKEN or not config.TELEGRAM_NOTIFY_CHAT_ID:
            return

        gift_id = str(lot["gift_id"])
        buy_price = _safe_float(lot.get("buy_price"))
        listed_price = _safe_float(lot.get("listed_price"))
        last_floor = _safe_float(lot.get("last_floor"))
        best_bid = _safe_float(lot.get("last_order_bid"))
        pl_percent = 0.0
        if buy_price > 0 and listed_price > 0:
            pl_percent = ((listed_price - buy_price) / buy_price) * 100.0

        text = (
            "⚠️ <b>Auto-Sell: критическая ситуация</b>\n\n"
            f"Причина: {html.escape(reason_text)}\n"
            f"Подарок: <b>{html.escape(str(lot.get('collection_title') or lot.get('collection_name') or gift_id))}</b>\n"
            f"ID: <code>{html.escape(gift_id)}</code>\n"
            f"buy_price: <b>{buy_price:.4f} TON</b>\n"
            f"current listed: <b>{listed_price:.4f} TON</b>\n"
            f"current floor: <b>{last_floor:.4f} TON</b>\n"
            f"best order: <b>{best_bid:.4f} TON</b>\n"
            f"P/L: <b>{pl_percent:+.2f}%</b>\n"
            f"Изменений цены: <b>{int(lot.get('relist_count') or 0)}/"
            f"{int(config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE) + int(lot.get('extra_changes') or 0)}</b>\n\n"
            "Если не ответить, действие по умолчанию: HOLD."
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Холд", "callback_data": f"sellp:{prompt_id}:hold"},
                    {"text": "Продать сейчас", "callback_data": f"sellp:{prompt_id}:sell"},
                ],
                [
                    {"text": "Докупить +5", "callback_data": f"sellp:{prompt_id}:buy5"},
                ],
                [
                    {"text": "30м", "callback_data": f"sellp:{prompt_id}:e30"},
                    {"text": "1ч", "callback_data": f"sellp:{prompt_id}:e60"},
                    {"text": "2ч", "callback_data": f"sellp:{prompt_id}:e120"},
                    {"text": "6ч", "callback_data": f"sellp:{prompt_id}:e360"},
                ],
                [
                    {"text": "Ввести свое время", "callback_data": f"sellp:{prompt_id}:custom"},
                ],
            ]
        }
        payload = {
            "chat_id": config.TELEGRAM_NOTIFY_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(reply_markup, ensure_ascii=True),
        }
        url = f"https://api.telegram.org/bot{config.TELEGRAM_NOTIFY_BOT_TOKEN}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload, timeout=8) as resp:
                    if not resp.ok:
                        logger.error(
                            "Failed to send critical prompt %s: %s %s",
                            prompt_id,
                            resp.status,
                            await resp.text(),
                        )
        except Exception as exc:
            logger.error("Failed to send critical prompt %s: %s", prompt_id, exc)

    async def _handle_prompt_timeouts(self, now_ts: int) -> None:
        for prompt in self.store.list_expired_open_prompts(now_ts):
            prompt_id = int(prompt["id"])
            default_action = str(prompt.get("default_on_timeout") or "hold")
            self.store.resolve_prompt(
                prompt_id,
                action=default_action,
                source="AUTO_TIMEOUT",
            )
            self.store.log_action(
                str(prompt.get("gift_id") or ""),
                "critical_prompt_timeout",
                "AUTO",
                {"prompt_id": prompt_id, "default_action": default_action},
            )

    async def _handle_prompt_actions(
        self,
        settings: SellRuntimeSettings,
        now_ts: int,
    ) -> None:
        prompts = self.store.list_unhandled_resolved_prompts()
        for prompt in prompts:
            prompt_id = int(prompt["id"])
            gift_id = str(prompt["gift_id"])
            action = str(prompt.get("resolved_action") or "hold")
            lot = self.store.get_lot(gift_id)

            if not lot:
                self.store.mark_prompt_handled(prompt_id)
                continue

            if action == "hold":
                self.store.update_lot(gift_id, status="HOLD")
                self.store.log_action(
                    gift_id,
                    "prompt_action_hold",
                    str(prompt.get("resolved_by") or "UNKNOWN"),
                    {"prompt_id": prompt_id},
                )
            elif action == "sell_now":
                await self._execute_sell_now(lot, settings, now_ts, prompt_id)
            elif action == "buy5":
                await self._execute_buy_more_changes(lot, prompt_id)
            elif action.startswith("extend:"):
                minutes = max(1, int(action.split(":", 1)[1]))
                self.store.update_lot(
                    gift_id,
                    status="LISTED",
                    next_critical_at=now_ts + minutes * 60,
                )
                self.store.log_action(
                    gift_id,
                    "prompt_action_extend",
                    str(prompt.get("resolved_by") or "UNKNOWN"),
                    {"prompt_id": prompt_id, "minutes": minutes},
                )
            else:
                self.store.update_lot(gift_id, status="HOLD")
                self.store.log_action(
                    gift_id,
                    "prompt_action_unknown_hold",
                    str(prompt.get("resolved_by") or "UNKNOWN"),
                    {"prompt_id": prompt_id, "action": action},
                )

            self.store.mark_prompt_handled(prompt_id)

    async def _execute_buy_more_changes(self, lot: dict[str, Any], prompt_id: int) -> None:
        gift_id = str(lot["gift_id"])
        result = await self.client.buy_more_sell_changes([gift_id])
        if result:
            new_extra = int(lot.get("extra_changes") or 0) + 5
            self.store.update_lot(gift_id, extra_changes=new_extra, status="LISTED")
            self.store.log_action(
                gift_id,
                "prompt_action_buy5_success",
                "TG_USER",
                {"prompt_id": prompt_id, "extra_changes": new_extra},
            )
        else:
            self.store.update_lot(gift_id, status="HOLD")
            self.store.log_action(
                gift_id,
                "prompt_action_buy5_failed_hold",
                "TG_USER",
                {"prompt_id": prompt_id},
            )

    async def _execute_sell_now(
        self,
        lot: dict[str, Any],
        settings: SellRuntimeSettings,
        now_ts: int,
        prompt_id: int,
    ) -> None:
        gift_id = str(lot["gift_id"])
        buy_price = _safe_float(lot.get("buy_price"))
        min_price = self._compute_stop_loss_price_ton(lot, settings)

        order_id: str | None = None
        best_bid: float | None = None
        if settings.order_mode_critical_only:
            payload = await self.client.get_top_order(
                collection_name=str(lot.get("collection_name") or ""),
                model_name=str(lot.get("model_name") or "") or None,
            )
            order_id, best_bid = _parse_best_order_payload(payload)
            self.store.update_lot(gift_id, last_order_bid=best_bid)

        if order_id and best_bid is not None and best_bid >= min_price:
            fill_result = await self.client.fill_order(order_id=order_id, gift_ids=[gift_id])
            if fill_result:
                self.store.mark_sold(gift_id, sold_price_ton=best_bid)
                self.store.log_action(
                    gift_id,
                    "prompt_action_sell_now_fill_order",
                    "TG_USER",
                    {
                        "prompt_id": prompt_id,
                        "order_id": order_id,
                        "best_bid": best_bid,
                        "buy_price": buy_price,
                    },
                )
                return

        # Fallback: relist at fastest allowed price (within stop-loss cap).
        floor_ton = _safe_float(lot.get("last_floor"), 0.0)
        if floor_ton > 0:
            target = max(min_price, floor_ton * 0.995)
        else:
            target = max(min_price, buy_price)
        target = max(target, AUTO_SELL_MIN_MARKET_PRICE_TON)

        changed = await self.client.change_sale_price([gift_id], target)
        if not changed:
            changed = await self.client.sell_gifts([gift_id], target)

        if changed:
            self.store.update_lot(
                gift_id,
                status="LISTED",
                listed_price=target,
                last_action_at=now_ts,
            )
            self.store.log_action(
                gift_id,
                "prompt_action_sell_now_relist",
                "TG_USER",
                {"prompt_id": prompt_id, "target_price": target, "min_price": min_price},
            )
        else:
            self.store.update_lot(gift_id, status="HOLD")
            self.store.log_action(
                gift_id,
                "prompt_action_sell_now_failed_hold",
                "TG_USER",
                {"prompt_id": prompt_id},
            )
