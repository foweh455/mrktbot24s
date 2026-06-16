from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from api import MRKTClient
from autosell_store import AutoSellStore
from state import state as app_state
from text_normalizer import normalize_text

logger = logging.getLogger("mrkt.tg_bot")
sell_store = AutoSellStore()
TEST_LISTING_PRICE_TON = 30.0


if config.TELEGRAM_NOTIFY_BOT_TOKEN:
    bot = Bot(token=config.TELEGRAM_NOTIFY_BOT_TOKEN)
    dp = Dispatcher()
else:
    bot = None
    dp = None


class Form(StatesGroup):
    waiting_for_balance = State()
    waiting_for_discount_cheap = State()
    waiting_for_discount_expensive = State()
    waiting_for_discount_10_20 = State()
    waiting_for_discount_20_plus = State()

    waiting_for_rare_balance = State()
    waiting_for_rare_premium_5_10 = State()
    waiting_for_rare_premium_10_plus = State()

    waiting_for_sell_balance = State()
    waiting_for_sell_profit_5_10 = State()
    waiting_for_sell_profit_10_plus = State()
    waiting_for_sell_floor_premium_5_10 = State()
    waiting_for_sell_floor_premium_10_plus = State()
    waiting_for_sell_relist_interval_minutes = State()
    waiting_for_sell_stuck_hours = State()
    waiting_for_sell_stop_loss = State()
    waiting_for_sell_prompt_threshold = State()
    waiting_for_sell_rare_protect = State()
    waiting_for_sell_custom_minutes = State()

    waiting_for_zero_rarity_threshold = State()
    waiting_for_floor_refresh_seconds = State()


def is_allowed_chat(chat_id: int | str | None) -> bool:
    if chat_id is None:
        return False
    return str(chat_id) == str(config.TELEGRAM_NOTIFY_CHAT_ID)


def _status_text(enabled: bool) -> str:
    return "ВКЛЮЧЕН" if enabled else "ВЫКЛЮЧЕН"


def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=normalize_text(text), callback_data=callback_data)


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"Статус: {_status_text(app_state.is_running)}", "toggle_status")],
            [_btn(f"Баланс: {app_state.balance:.2f} TON", "set_balance")],
            [_btn(f"Скидка (<{app_state.threshold:g}T): {app_state.discount_cheap:.1f}%", "set_discount_cheap")],
            [_btn(f"Скидка (>={app_state.threshold:g}T): {app_state.discount_expensive:.1f}%", "set_discount_expensive")],
            [_btn(f"Скидка (10-20T): {app_state.discount_10_20:.1f}%", "set_discount_10_20")],
            [_btn(f"Скидка (20T+): {app_state.discount_20_plus:.1f}%", "set_discount_20_plus")],
            [_btn(f"Анти-дамп: {_status_text(bool(getattr(app_state, 'buy_depth_guard_enabled', False)))}", "toggle_buy_depth_guard")],
            [_btn(
                f"Zero-rarity гейт: "
                f"{'ON' if app_state.zero_rarity_gate_enabled else 'OFF'} "
                f"({app_state.zero_rarity_min_discount:.1f}%)",
                "toggle_zero_rarity_gate",
            )],
            [_btn(
                f"Zero-rarity порог: {app_state.zero_rarity_min_discount:.1f}%",
                "set_zero_rarity_threshold",
            )],
            [_btn(
                f"Floor refresh: {app_state.floor_refresh_seconds:.1f}s",
                "set_floor_refresh_seconds",
            )],
            [_btn("Тест: выставить за 30 TON", "test_list_30_ton")],
            [_btn("Статистика снайпера", "show_sniper_stats")],
            [_btn("Меню редких моделей", "open_rare_menu")],
            [_btn("Меню автопродажи", "open_sell_menu")],
        ]
    )


def build_stats_text() -> str:
    best_date = str(getattr(app_state, "stat_missed_balance_best_day_date", "") or "")
    best_discount = float(
        getattr(app_state, "stat_missed_balance_best_day_discount", 0.0) or 0.0
    )
    best_price = float(getattr(app_state, "stat_missed_balance_best_day_price", 0.0) or 0.0)
    best_floor = float(getattr(app_state, "stat_missed_balance_best_day_floor", 0.0) or 0.0)
    best_collection = str(
        getattr(app_state, "stat_missed_balance_best_day_collection", "") or ""
    ).strip()

    if best_date and best_discount > 0.0 and best_price > 0.0:
        best_line = (
            f"Лучший по отклонению от флора за день ({best_date}, UTC): "
            f"{best_discount:.2f}% | цена {best_price:.2f} TON"
        )
        if best_floor > 0.0:
            best_line += f" | флор {best_floor:.2f} TON"
        if best_collection:
            best_line += f" | {best_collection}"
    else:
        best_line = "Лучший по отклонению от флора за день (8+): пока нет данных"

    return (
        "Статистика снайпера\n\n"
        f"1) Упущено: лот уже купили до нас: {int(app_state.stat_missed_bought_before)}\n"
        f"2) Упущено из-за баланса (цена > 8 TON и скидка >= 3%): "
        f"{int(app_state.stat_missed_balance_high_value)}\n"
        f"   8-20 TON: {int(getattr(app_state, 'stat_missed_balance_8_20', 0))}\n"
        f"   20-50 TON: {int(getattr(app_state, 'stat_missed_balance_20_50', 0))}\n"
        f"   50+ TON: {int(getattr(app_state, 'stat_missed_balance_50_plus', 0))}\n"
        f"3) {best_line}"
    )

def get_rare_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"Режим редких моделей: {_status_text(app_state.rare_enabled)}", "toggle_rare_status")],
            [_btn(f"Баланс rare: {app_state.rare_balance:.2f} TON", "set_rare_balance")],
            [_btn(f"Premium (5-10T): {app_state.rare_premium_5_10:.1f}%", "set_rare_premium_5_10")],
            [_btn(f"Premium (10T+): {app_state.rare_premium_10_plus:.1f}%", "set_rare_premium_10_plus")],
            [_btn(f"Мин. цена fixed: {app_state.rare_min_listing_price:.1f} TON", "rare_info_min_price")],
            [_btn(f"Редкость модели: <= {app_state.rare_max_rarity_percent:.2f}%", "rare_info_rarity")],
            [_btn("Назад в основное меню", "back_to_main_menu")],
        ]
    )


def get_sell_keyboard() -> InlineKeyboardMarkup:
    order_mode = "Критика only" if app_state.sell_order_mode_critical_only else "Выключено"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"Автопродажа: {_status_text(app_state.sell_enabled)}", "toggle_sell_status")],
            [_btn(f"Баланс автопродажи: {app_state.sell_balance:.2f} TON", "set_sell_balance")],
            [_btn(f"Профит 5-10T: {app_state.sell_target_profit_5_10:.1f}%", "set_sell_profit_5_10")],
            [_btn(f"Профит 10T+: {app_state.sell_target_profit_10_plus:.1f}%", "set_sell_profit_10_plus")],
            [_btn(f"Премия к флору 5-10T: {app_state.sell_floor_premium_5_10:.1f}%", "set_sell_floor_premium_5_10")],
            [_btn(f"Премия к флору 10T+: {app_state.sell_floor_premium_10_plus:.1f}%", "set_sell_floor_premium_10_plus")],
            [_btn(f"Релист: {int(app_state.sell_relist_interval_seconds // 60)} мин", "set_sell_relist_interval")],
            [_btn(f"Горизонт: {app_state.sell_stuck_hours:.1f}ч", "set_sell_stuck_hours")],
            [_btn(f"Лимит убытка: -{app_state.sell_loss_cap_percent:.1f}%", "set_sell_stop_loss")],
            [_btn(f"Критика при цене >= {app_state.sell_prompt_price_threshold:.1f} TON", "set_sell_prompt_threshold")],
            [_btn(f"Не продавать авто при редкости <= {app_state.sell_rare_protect_percent:.2f}%", "set_sell_rare_protect")],
            [_btn(f"Режим ордеров: {order_mode}", "toggle_sell_order_mode")],
            [_btn(
                f"+5 изменений цены только вручную (база {config.AUTO_SELL_PRICE_CHANGE_LIMIT_BASE})",
                "sell_info_buy5",
            )],
            [_btn("Назад в основное меню", "back_to_main_menu_from_sell")],
        ]
    )


async def safe_edit_keyboard(call: CallbackQuery, markup: InlineKeyboardMarkup) -> None:
    if call.message:
        await call.message.edit_reply_markup(reply_markup=markup)


async def _answer_message(message: Message, text: str, **kwargs) -> None:
    await message.answer(normalize_text(text), **kwargs)


async def _answer_call(call: CallbackQuery, text: str | None = None, **kwargs) -> None:
    if text is not None:
        text = normalize_text(text)
    await call.answer(text, **kwargs)


def _parse_float_message(message: Message) -> float:
    return float((message.text or "").strip().replace(",", "."))


def _parse_int_message(message: Message) -> int:
    return int((message.text or "").strip())


async def _ask_value(call: CallbackQuery, state: FSMContext, next_state: State, text: str) -> None:
    if not call.message or not is_allowed_chat(call.message.chat.id):
        await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
        return
    await state.set_state(next_state)
    await _answer_message(call.message, text)
    await _answer_call(call)


async def _fetch_unlisted_gifts() -> list[dict]:
    client = MRKTClient()
    try:
        data = await client.get_my_gifts(is_listed=False, count=100, cursor="")
        gifts = data.get("gifts", []) if isinstance(data, dict) else []
        return gifts if isinstance(gifts, list) else []
    finally:
        await client.close()


def _pick_test_gift(gifts: list[dict]) -> dict | None:
    if not gifts:
        return None
    by_id = {str(g.get("id")): g for g in gifts if isinstance(g, dict) and g.get("id")}
    for gift_id in sell_store.list_recent_purchase_ids(limit=20):
        gift = by_id.get(str(gift_id))
        if gift is not None:
            return gift
    for gift in gifts:
        if isinstance(gift, dict) and gift.get("id"):
            return gift
    return None


async def _list_test_gift(gift_id: str, price_ton: float) -> bool:
    client = MRKTClient()
    try:
        result = await client.sell_gifts([gift_id], price_ton)
        return bool(result)
    finally:
        await client.close()


SETTING_CALLBACKS: dict[str, tuple[State, str]] = {
    "set_balance": (Form.waiting_for_balance, "Р’РІРµРґРёС‚Рµ Р±Р°Р»Р°РЅСЃ РѕР±С‹С‡РЅРѕРіРѕ СЂРµР¶РёРјР° (TON), РЅР°РїСЂРёРјРµСЂ: 10.5"),
    "set_discount_cheap": (Form.waiting_for_discount_cheap, "Р’РІРµРґРёС‚Рµ СЃРєРёРґРєСѓ РґР»СЏ <4 TON, РЅР°РїСЂРёРјРµСЂ: 3.5"),
    "set_discount_expensive": (Form.waiting_for_discount_expensive, "Р’РІРµРґРёС‚Рµ СЃРєРёРґРєСѓ РґР»СЏ >=4 TON, РЅР°РїСЂРёРјРµСЂ: 3.0"),
    "set_discount_10_20": (Form.waiting_for_discount_10_20, "Р’РІРµРґРёС‚Рµ СЃРєРёРґРєСѓ РґР»СЏ 10-20 TON, РЅР°РїСЂРёРјРµСЂ: 2.5"),
    "set_discount_20_plus": (Form.waiting_for_discount_20_plus, "Р’РІРµРґРёС‚Рµ СЃРєРёРґРєСѓ РґР»СЏ 20+ TON, РЅР°РїСЂРёРјРµСЂ: 2.0"),
    "set_rare_balance": (Form.waiting_for_rare_balance, "Р’РІРµРґРёС‚Рµ rare-Р±Р°Р»Р°РЅСЃ (TON), РЅР°РїСЂРёРјРµСЂ: 7.5"),
    "set_rare_premium_5_10": (Form.waiting_for_rare_premium_5_10, "Р’РІРµРґРёС‚Рµ premium РґР»СЏ rare 5-10 TON, РЅР°РїСЂРёРјРµСЂ: 8.0"),
    "set_rare_premium_10_plus": (Form.waiting_for_rare_premium_10_plus, "Р’РІРµРґРёС‚Рµ premium РґР»СЏ rare 10+ TON, РЅР°РїСЂРёРјРµСЂ: 10.0"),
    "set_sell_balance": (Form.waiting_for_sell_balance, "Р’РІРµРґРёС‚Рµ Р±Р°Р»Р°РЅСЃ Р°РІС‚РѕРїСЂРѕРґР°Р¶Рё (TON), РЅР°РїСЂРёРјРµСЂ: 1000"),
    "set_sell_profit_5_10": (Form.waiting_for_sell_profit_5_10, "Р’РІРµРґРёС‚Рµ РїСЂРѕС„РёС‚ (%) РґР»СЏ 5-10 TON, РЅР°РїСЂРёРјРµСЂ: 2.0"),
    "set_sell_profit_10_plus": (Form.waiting_for_sell_profit_10_plus, "Р’РІРµРґРёС‚Рµ РїСЂРѕС„РёС‚ (%) РґР»СЏ 10+ TON, РЅР°РїСЂРёРјРµСЂ: 2.0"),
    "set_sell_floor_premium_5_10": (Form.waiting_for_sell_floor_premium_5_10, "Р’РІРµРґРёС‚Рµ premium Рє С„Р»РѕСЂСѓ (%) РґР»СЏ 5-10 TON, РЅР°РїСЂРёРјРµСЂ: 1.5"),
    "set_sell_floor_premium_10_plus": (Form.waiting_for_sell_floor_premium_10_plus, "Р’РІРµРґРёС‚Рµ premium Рє С„Р»РѕСЂСѓ (%) РґР»СЏ 10+ TON, РЅР°РїСЂРёРјРµСЂ: 1.0"),
    "set_sell_relist_interval": (Form.waiting_for_sell_relist_interval_minutes, "Р’РІРµРґРёС‚Рµ РёРЅС‚РµСЂРІР°Р» СЂРµР»РёСЃС‚Р° (РјРёРЅСѓС‚С‹), РЅР°РїСЂРёРјРµСЂ: 10"),
    "set_sell_stuck_hours": (Form.waiting_for_sell_stuck_hours, "Р’РІРµРґРёС‚Рµ РіРѕСЂРёР·РѕРЅС‚ РєСЂРёС‚РёРєРё (С‡Р°СЃС‹), РЅР°РїСЂРёРјРµСЂ: 2"),
    "set_sell_stop_loss": (Form.waiting_for_sell_stop_loss, "Р’РІРµРґРёС‚Рµ max СѓР±С‹С‚РѕРє РІ %, РЅР°РїСЂРёРјРµСЂ: 4"),
    "set_sell_prompt_threshold": (Form.waiting_for_sell_prompt_threshold, "Р’РІРµРґРёС‚Рµ РїРѕСЂРѕРі С†РµРЅС‹ РґР»СЏ РєСЂРёС‚РёРєРё (TON), РЅР°РїСЂРёРјРµСЂ: 4"),
    "set_sell_rare_protect": (Form.waiting_for_sell_rare_protect, "Р’РІРµРґРёС‚Рµ РїРѕСЂРѕРі СЂРµРґРєРѕСЃС‚Рё (%), РЅР°РїСЂРёРјРµСЂ: 1.0"),
    "set_zero_rarity_threshold": (
        Form.waiting_for_zero_rarity_threshold,
        "Введите порог скидки zero-rarity в % (0.0..100.0), например: 30.0",
    ),
    "set_floor_refresh_seconds": (
        Form.waiting_for_floor_refresh_seconds,
        f"Введите интервал обновления флора в секундах "
        f"(не меньше {config.FLOOR_REFRESH_MIN_SECONDS:g}), например: 12.0",
    ),
}


FLOAT_STATE_CONFIG: dict[str, tuple[str, float, str, Callable[[], InlineKeyboardMarkup]]] = {
    Form.waiting_for_balance.state: ("balance", 0.0000001, "Р‘Р°Р»Р°РЅСЃ РѕР±РЅРѕРІР»РµРЅ.", get_main_keyboard),
    Form.waiting_for_discount_cheap.state: ("discount_cheap", 0.0, "РЎРєРёРґРєР° <4T РѕР±РЅРѕРІР»РµРЅР°.", get_main_keyboard),
    Form.waiting_for_discount_expensive.state: ("discount_expensive", 0.0, "РЎРєРёРґРєР° >=4T РѕР±РЅРѕРІР»РµРЅР°.", get_main_keyboard),
    Form.waiting_for_discount_10_20.state: ("discount_10_20", 0.0, "РЎРєРёРґРєР° 10-20T РѕР±РЅРѕРІР»РµРЅР°.", get_main_keyboard),
    Form.waiting_for_discount_20_plus.state: ("discount_20_plus", 0.0, "РЎРєРёРґРєР° 20+T РѕР±РЅРѕРІР»РµРЅР°.", get_main_keyboard),
    Form.waiting_for_rare_balance.state: ("rare_balance", 0.0000001, "Rare balance РѕР±РЅРѕРІР»РµРЅ.", get_rare_keyboard),
    Form.waiting_for_rare_premium_5_10.state: ("rare_premium_5_10", 0.0, "Premium rare 5-10T РѕР±РЅРѕРІР»РµРЅ.", get_rare_keyboard),
    Form.waiting_for_rare_premium_10_plus.state: ("rare_premium_10_plus", 0.0, "Premium rare 10+T РѕР±РЅРѕРІР»РµРЅ.", get_rare_keyboard),
    Form.waiting_for_sell_balance.state: ("sell_balance", 0.0000001, "Р‘Р°Р»Р°РЅСЃ Р°РІС‚РѕРїСЂРѕРґР°Р¶Рё РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_profit_5_10.state: ("sell_target_profit_5_10", 0.0, "РџСЂРѕС„РёС‚ 5-10T РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_profit_10_plus.state: ("sell_target_profit_10_plus", 0.0, "РџСЂРѕС„РёС‚ 10+T РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_floor_premium_5_10.state: ("sell_floor_premium_5_10", 0.0, "Premium Рє С„Р»РѕСЂСѓ 5-10T РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_floor_premium_10_plus.state: ("sell_floor_premium_10_plus", 0.0, "Premium Рє С„Р»РѕСЂСѓ 10+T РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_stuck_hours.state: ("sell_stuck_hours", 0.1, "Р“РѕСЂРёР·РѕРЅС‚ РєСЂРёС‚РёРєРё РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_stop_loss.state: ("sell_loss_cap_percent", 0.0, "Р›РёРјРёС‚ СѓР±С‹С‚РєР° РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_prompt_threshold.state: ("sell_prompt_price_threshold", 0.0, "РџРѕСЂРѕРі РєСЂРёС‚РёРєРё РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_sell_rare_protect.state: ("sell_rare_protect_percent", 0.0, "РџРѕСЂРѕРі Р·Р°С‰РёС‚С‹ СЂРµРґРєРѕСЃС‚Рё РѕР±РЅРѕРІР»РµРЅ.", get_sell_keyboard),
    Form.waiting_for_zero_rarity_threshold.state: (
        "zero_rarity_min_discount",
        0.0,
        "Порог zero-rarity обновлён.",
        get_main_keyboard,
    ),
    Form.waiting_for_floor_refresh_seconds.state: (
        "floor_refresh_seconds",
        float(config.FLOOR_REFRESH_MIN_SECONDS),
        "Интервал обновления флора обновлён.",
        get_main_keyboard,
    ),
}


if dp:
    @dp.message(Command("start", "menu"))
    async def start_cmd(message: Message, state: FSMContext) -> None:
        if not is_allowed_chat(message.chat.id):
            return
        await state.clear()
        await _answer_message(message, 
            "РџР°РЅРµР»СЊ СѓРїСЂР°РІР»РµРЅРёСЏ MRKT Sniper\n"
            "РћР±С‹С‡РЅС‹Р№ СЂРµР¶РёРј: СЃРєРёРґРєРё РїРѕ С„Р»РѕСЂСѓ.\n"
            "РћС‚РґРµР»СЊРЅС‹Рµ СЂРµР¶РёРјС‹: /rare Рё /sell.",
            reply_markup=get_main_keyboard(),
        )

    @dp.message(Command("rare"))
    async def rare_cmd(message: Message, state: FSMContext) -> None:
        if not is_allowed_chat(message.chat.id):
            return
        await state.clear()
        await _answer_message(message, 
            "Р РµР¶РёРј СЂРµРґРєРёС… РјРѕРґРµР»РµР№ (РѕС‚РґРµР»СЊРЅС‹Р№).\n"
            "РџРѕСЃР»Рµ РєР°Р¶РґРѕРіРѕ СЂРµСЃС‚Р°СЂС‚Р° РѕРЅ РІС‹РєР»СЋС‡РµРЅ.\n"
            "РњРёРЅРёРјР°Р»СЊРЅР°СЏ С†РµРЅР°: 5 TON (С„РёРєСЃ).",
            reply_markup=get_rare_keyboard(),
        )

    @dp.message(Command("stats"))
    async def stats_cmd(message: Message, state: FSMContext) -> None:
        if not is_allowed_chat(message.chat.id):
            return
        await state.clear()
        await _answer_message(message, build_stats_text(), reply_markup=get_main_keyboard())

    @dp.message(Command("sell"))
    async def sell_cmd(message: Message, state: FSMContext) -> None:
        if not is_allowed_chat(message.chat.id):
            return
        await state.clear()
        await _answer_message(message, 
            "Р РµР¶РёРј Р°РІС‚РѕРїСЂРѕРґР°Р¶Рё (РїРѕР»РЅРѕСЃС‚СЊСЋ РѕС‚РґРµР»СЊРЅС‹Р№).\n"
            "РћР±СЂР°Р±Р°С‚С‹РІР°РµС‚ С‚РѕР»СЊРєРѕ РїРѕРєСѓРїРєРё СЌС‚РѕРіРѕ Р±РѕС‚Р°.",
            reply_markup=get_sell_keyboard(),
        )

    @dp.callback_query(F.data == "toggle_status")
    async def toggle_status(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        app_state.is_running = not app_state.is_running
        await safe_edit_keyboard(call, get_main_keyboard())
        await _answer_call(call, "РЎС‚Р°С‚СѓСЃ РёР·РјРµРЅРµРЅ.")

    @dp.callback_query(F.data == "toggle_buy_depth_guard")
    async def toggle_buy_depth_guard(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "Недоступно", show_alert=True)
            return
        app_state.buy_depth_guard_enabled = not bool(
            getattr(app_state, "buy_depth_guard_enabled", False)
        )
        await safe_edit_keyboard(call, get_main_keyboard())
        status_text = "включен" if app_state.buy_depth_guard_enabled else "выключен"
        await _answer_call(
            call,
            f"Анти-дамп {status_text}. Лимит: <= {int(getattr(app_state, 'buy_depth_guard_max_at_or_below', 3))} лота на/ниже цены.",
        )

    @dp.callback_query(F.data == "toggle_zero_rarity_gate")
    async def toggle_zero_rarity_gate(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "Недоступно", show_alert=True)
            return
        app_state.zero_rarity_gate_enabled = not bool(
            getattr(app_state, "zero_rarity_gate_enabled", True)
        )
        await safe_edit_keyboard(call, get_main_keyboard())
        status = "включен" if app_state.zero_rarity_gate_enabled else "выключен"
        await _answer_call(
            call,
            f"Zero-rarity гейт {status}. Порог: {app_state.zero_rarity_min_discount:.1f}%.",
        )

    @dp.callback_query(F.data == "show_sniper_stats")
    async def show_sniper_stats(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "Недоступно", show_alert=True)
            return
        await _answer_call(call, "Статистика обновлена.")
        await _answer_message(call.message, build_stats_text(), reply_markup=get_main_keyboard())

    @dp.callback_query(F.data == "open_rare_menu")
    async def open_rare_menu(call: CallbackQuery, state: FSMContext) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await state.clear()
        await safe_edit_keyboard(call, get_rare_keyboard())
        await _answer_call(call, "РћС‚РєСЂС‹С‚Рѕ РјРµРЅСЋ СЂРµРґРєРёС… РјРѕРґРµР»РµР№.")

    @dp.callback_query(F.data == "open_sell_menu")
    async def open_sell_menu(call: CallbackQuery, state: FSMContext) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await state.clear()
        await safe_edit_keyboard(call, get_sell_keyboard())
        await _answer_call(call, "РћС‚РєСЂС‹С‚Рѕ РјРµРЅСЋ Р°РІС‚РѕРїСЂРѕРґР°Р¶Рё.")

    @dp.callback_query(F.data.in_({"back_to_main_menu", "back_to_main_menu_from_sell"}))
    async def back_to_main_menu(call: CallbackQuery, state: FSMContext) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await state.clear()
        await safe_edit_keyboard(call, get_main_keyboard())
        await _answer_call(call, "Р’РµСЂРЅСѓР»РёСЃСЊ РІ РѕСЃРЅРѕРІРЅРѕРµ РјРµРЅСЋ.")

    @dp.callback_query(F.data == "toggle_rare_status")
    async def toggle_rare_status(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        app_state.rare_enabled = not app_state.rare_enabled
        await safe_edit_keyboard(call, get_rare_keyboard())
        await _answer_call(call, "Р РµР¶РёРј СЂРµРґРєРёС… РјРѕРґРµР»РµР№ РїРµСЂРµРєР»СЋС‡РµРЅ.")

    @dp.callback_query(F.data == "toggle_sell_status")
    async def toggle_sell_status(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        app_state.sell_enabled = not app_state.sell_enabled
        await safe_edit_keyboard(call, get_sell_keyboard())
        await _answer_call(call, "Р РµР¶РёРј Р°РІС‚РѕРїСЂРѕРґР°Р¶Рё РїРµСЂРµРєР»СЋС‡РµРЅ.")

    @dp.callback_query(F.data == "toggle_sell_order_mode")
    async def toggle_sell_order_mode(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        app_state.sell_order_mode_critical_only = not app_state.sell_order_mode_critical_only
        await safe_edit_keyboard(call, get_sell_keyboard())
        await _answer_call(call, "Р РµР¶РёРј РѕСЂРґРµСЂРѕРІ РѕР±РЅРѕРІР»РµРЅ.")

    @dp.callback_query(F.data == "sell_info_buy5")
    async def sell_info_buy5(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await _answer_call(call, "РђРІС‚РѕРґРѕРєСѓРїРєР° +5 РёР·РјРµРЅРµРЅРёР№ РѕС‚РєР»СЋС‡РµРЅР°, С‚РѕР»СЊРєРѕ РІСЂСѓС‡РЅСѓСЋ РІ РєСЂРёС‚РёС‡РµСЃРєРѕР№ РєР°СЂС‚РѕС‡РєРµ.", show_alert=True)

    @dp.callback_query(F.data == "rare_info_min_price")
    async def rare_info_min_price(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await _answer_call(call, "Р’ rare-СЂРµР¶РёРјРµ РјРёРЅРёРјР°Р»СЊРЅР°СЏ С†РµРЅР° РІСЃРµРіРґР° 5 TON.", show_alert=True)

    @dp.callback_query(F.data == "rare_info_rarity")
    async def rare_info_rarity(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await _answer_call(call, 
            f"РџРѕСЂРѕРі СЂРµРґРєРѕСЃС‚Рё: <= {app_state.rare_max_rarity_percent:.2f}%",
            show_alert=True,
        )

    @dp.callback_query(F.data.in_(set(SETTING_CALLBACKS.keys())))
    async def ask_setting_value(call: CallbackQuery, state: FSMContext) -> None:
        setup = SETTING_CALLBACKS.get(call.data or "")
        if not setup:
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        next_state, text = setup
        await _ask_value(call, state, next_state, text)

    @dp.callback_query(F.data == "test_list_30_ton")
    async def test_list_30_ton(call: CallbackQuery) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return
        await _answer_call(call)
        await _answer_message(call.message, "РС‰Сѓ РїРѕРґР°СЂРѕРє Рё РїСЂРѕР±СѓСЋ РІС‹СЃС‚Р°РІРёС‚СЊ Р·Р° 30 TON...")
        try:
            now_ts = int(time.time())
            recent_purchases = sell_store.list_recent_purchases(limit=20)

            for purchase in recent_purchases:
                gift_id = str(purchase.get("gift_id") or "")
                if not gift_id:
                    continue

                bought_at = int(purchase.get("bought_at") or 0)
                if bought_at and now_ts < bought_at + 60:
                    continue

                if await _list_test_gift(gift_id, TEST_LISTING_PRICE_TON):
                    sell_store.log_action(
                        gift_id,
                        "manual_test_list_30_ton",
                        "TG_USER",
                        {"price_ton": TEST_LISTING_PRICE_TON},
                    )
                    if sell_store.get_lot(gift_id):
                        sell_store.update_lot(
                            gift_id,
                            status="LISTED",
                            listed_price=TEST_LISTING_PRICE_TON,
                        )
                    title = str(
                        purchase.get("collection_title")
                        or purchase.get("collection_name")
                        or gift_id
                    )
                    await _answer_message(call.message, 
                        "РўРµСЃС‚РѕРІР°СЏ РІС‹СЃС‚Р°РІРєР° РІС‹РїРѕР»РЅРµРЅР°.\n"
                        f"РџРѕРґР°СЂРѕРє: {title}\n"
                        f"ID: {gift_id}",
                    )
                    return

            if recent_purchases:
                newest = recent_purchases[0]
                newest_wait_left = max(
                    0,
                    int(newest.get("bought_at") or 0) + 60 - now_ts,
                )
                if newest_wait_left > 0:
                    await _answer_message(call.message, 
                        f"РџРѕСЃР»Рµ РїРѕРєСѓРїРєРё РґРµР№СЃС‚РІСѓРµС‚ РљР” 60 СЃРµРє. РџРѕРґРѕР¶РґРёС‚Рµ РµС‰Рµ ~{newest_wait_left} СЃРµРє Рё РїРѕРІС‚РѕСЂРёС‚Рµ."
                    )
                    return

            gifts = await _fetch_unlisted_gifts()
            picked = _pick_test_gift(gifts)
            if not picked:
                await _answer_message(call.message, "РќРµ РЅР°Р№РґРµРЅ РїРѕРґС…РѕРґСЏС‰РёР№ РїРѕРґР°СЂРѕРє РґР»СЏ С‚РµСЃС‚РѕРІРѕР№ РІС‹СЃС‚Р°РІРєРё.")
                return

            gift_id = str(picked.get("id"))
            if not await _list_test_gift(gift_id, TEST_LISTING_PRICE_TON):
                await _answer_message(call.message, "РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹СЃС‚Р°РІРёС‚СЊ Р·Р° 30 TON (РѕС€РёР±РєР° API).")
                return

            sell_store.log_action(
                gift_id,
                "manual_test_list_30_ton",
                "TG_USER",
                {"price_ton": TEST_LISTING_PRICE_TON},
            )
            if sell_store.get_lot(gift_id):
                sell_store.update_lot(
                    gift_id,
                    status="LISTED",
                    listed_price=TEST_LISTING_PRICE_TON,
                )
            title = str(
                picked.get("collectionTitle")
                or picked.get("collectionName")
                or picked.get("title")
                or gift_id
            )
            await _answer_message(call.message, 
                "РўРµСЃС‚РѕРІР°СЏ РІС‹СЃС‚Р°РІРєР° РІС‹РїРѕР»РЅРµРЅР°.\n"
                f"РџРѕРґР°СЂРѕРє: {title}\n"
                f"ID: {gift_id}",
            )
        except Exception as exc:
            logger.exception("test_list_30_ton failed")
            await _answer_message(call.message, f"РћС€РёР±РєР°: {exc}")

    @dp.callback_query(F.data.startswith("sellp:"))
    async def sell_prompt_callback(call: CallbackQuery, state: FSMContext) -> None:
        if not call.message or not is_allowed_chat(call.message.chat.id):
            await _answer_call(call, "РќРµРґРѕСЃС‚СѓРїРЅРѕ", show_alert=True)
            return

        parts = (call.data or "").split(":")
        if len(parts) != 3:
            await _answer_call(call, "РќРµРІРµСЂРЅС‹Р№ callback", show_alert=True)
            return

        _, prompt_id_raw, action_code = parts
        try:
            prompt_id = int(prompt_id_raw)
        except ValueError:
            await _answer_call(call, "РќРµРІРµСЂРЅС‹Р№ prompt id", show_alert=True)
            return

        prompt = sell_store.get_prompt(prompt_id)
        if not prompt or str(prompt.get("status")) != "OPEN":
            await _answer_call(call, "Prompt СѓР¶Рµ Р·Р°РєСЂС‹С‚", show_alert=True)
            return

        action_map = {
            "hold": "hold",
            "sell": "sell_now",
            "buy5": "buy5",
            "e30": "extend:30",
            "e60": "extend:60",
            "e120": "extend:120",
            "e360": "extend:360",
        }

        if action_code == "custom":
            await state.set_state(Form.waiting_for_sell_custom_minutes)
            await state.update_data(prompt_id=prompt_id)
            await _answer_message(call.message, "Р’РІРµРґРёС‚Рµ РїСЂРѕРґР»РµРЅРёРµ РІ РјРёРЅСѓС‚Р°С…, РЅР°РїСЂРёРјРµСЂ: 90")
            await _answer_call(call)
            return

        action = action_map.get(action_code)
        if not action:
            await _answer_call(call, "РќРµРёР·РІРµСЃС‚РЅРѕРµ РґРµР№СЃС‚РІРёРµ", show_alert=True)
            return

        sell_store.resolve_prompt(prompt_id, action=action, source="TG_USER")
        sell_store.log_action(
            str(prompt.get("gift_id") or ""),
            "tg_prompt_resolve",
            "TG_USER",
            {"prompt_id": prompt_id, "action": action},
        )
        await _answer_call(call, "Р РµС€РµРЅРёРµ РїСЂРёРЅСЏС‚Рѕ.")
        await _answer_message(call.message, f"РџСЂРёРЅСЏС‚Рѕ РґРµР№СЃС‚РІРёРµ: {action}")

    @dp.message()
    async def process_state_input(message: Message, state: FSMContext) -> None:
        if not is_allowed_chat(message.chat.id):
            return

        current_state = await state.get_state()
        if not current_state:
            return

        if current_state == Form.waiting_for_sell_custom_minutes.state:
            data = await state.get_data()
            prompt_id = data.get("prompt_id")
            try:
                minutes = _parse_int_message(message)
                if minutes <= 0 or not isinstance(prompt_id, int):
                    raise ValueError

                prompt = sell_store.get_prompt(prompt_id)
                if not prompt or str(prompt.get("status")) != "OPEN":
                    await state.clear()
                    await _answer_message(message, "Р­С‚РѕС‚ prompt СѓР¶Рµ Р·Р°РєСЂС‹С‚.")
                    return

                action = f"extend:{minutes}"
                sell_store.resolve_prompt(prompt_id, action=action, source="TG_USER")
                sell_store.log_action(
                    str(prompt.get("gift_id") or ""),
                    "tg_prompt_resolve_custom_extend",
                    "TG_USER",
                    {"prompt_id": prompt_id, "minutes": minutes},
                )
                await state.clear()
                await _answer_message(message, f"РџСЂРѕРґР»РµРЅРёРµ РїСЂРёРЅСЏС‚Рѕ: {minutes} РјРёРЅ.")
            except Exception:
                await _answer_message(message, "РћС€РёР±РєР° РІРІРѕРґР°. Р’РІРµРґРёС‚Рµ С†РµР»РѕРµ С‡РёСЃР»Рѕ РјРёРЅСѓС‚, РЅР°РїСЂРёРјРµСЂ: 90")
            return

        if current_state == Form.waiting_for_sell_relist_interval_minutes.state:
            try:
                minutes = _parse_int_message(message)
                if minutes <= 0:
                    raise ValueError
                app_state.sell_relist_interval_seconds = float(minutes * 60)
                await state.clear()
                await _answer_message(message, "РРЅС‚РµСЂРІР°Р» СЂРµР»РёСЃС‚Р° РѕР±РЅРѕРІР»РµРЅ.", reply_markup=get_sell_keyboard())
            except Exception:
                await _answer_message(message, "РћС€РёР±РєР° РІРІРѕРґР°. Р’РІРµРґРёС‚Рµ С†РµР»РѕРµ С‡РёСЃР»Рѕ РјРёРЅСѓС‚, РЅР°РїСЂРёРјРµСЂ: 10")
            return

        cfg = FLOAT_STATE_CONFIG.get(current_state)
        if not cfg:
            return

        attr, min_value, ok_text, keyboard_builder = cfg
        try:
            value = _parse_float_message(message)
            if value < min_value:
                raise ValueError
            if attr == "zero_rarity_min_discount" and value > 100.0:
                await _answer_message(message, "Некорректное значение. Диапазон 0..100.")
                return
            setattr(app_state, attr, value)
            await state.clear()
            await _answer_message(message, ok_text, reply_markup=keyboard_builder())
        except Exception:
            await _answer_message(message, "РћС€РёР±РєР° РІРІРѕРґР°. Р’РІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅРѕРµ С‡РёСЃР»Рѕ.")


async def run_bot() -> None:
    if not bot or not dp:
        return

    logger.info("Starting Remote Control Telegram Bot...")
    await bot.delete_webhook(drop_pending_updates=True)

    while True:
        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_signals=False,
            )
        except asyncio.CancelledError:
            logger.info("Telegram controller stopped.")
            raise
        except Exception:
            logger.exception("Telegram polling failed, restarting in 5 seconds...")
            await asyncio.sleep(5)
        else:
            logger.warning("Telegram polling stopped unexpectedly, restarting in 2 seconds...")
            await asyncio.sleep(2)
