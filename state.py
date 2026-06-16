"""
Runtime mutable state.
Persists bot controls to disk so values survive restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import (
    AUTO_SELL_DEFAULT_BALANCE_TON,
    AUTO_SELL_DEFAULT_ENABLED,
    AUTO_SELL_FLOOR_PREMIUM_10_PLUS_PERCENT,
    AUTO_SELL_FLOOR_PREMIUM_5_10_PERCENT,
    AUTO_SELL_LOSS_CAP_PERCENT,
    AUTO_SELL_ORDER_MODE_CRITICAL_ONLY,
    AUTO_SELL_PRICE_THRESHOLD_TON,
    AUTO_SELL_PROMPT_PRICE_THRESHOLD_TON,
    AUTO_SELL_RARE_PROTECT_PERCENT,
    AUTO_SELL_RELIST_INTERVAL_SECONDS,
    AUTO_SELL_STUCK_HOURS,
    AUTO_SELL_TARGET_PROFIT_10_PLUS_PERCENT,
    AUTO_SELL_TARGET_PROFIT_5_10_PERCENT,
    AUTO_BUY_DISCOUNT_THRESHOLD_PRICE_TON,
    AUTO_BUY_DEPTH_GUARD_DEFAULT_ENABLED,
    AUTO_BUY_DEPTH_GUARD_MAX_AT_OR_BELOW,
    AUTO_BUY_DEPTH_GUARD_NEAR_PRICE_PERCENT,
    AUTO_BUY_DEPTH_GUARD_SCAN_COUNT,
    AUTO_BUY_MAX_PRICE_TON,
    AUTO_BUY_MIN_DISCOUNT_10_TO_20,
    AUTO_BUY_MIN_DISCOUNT_20_PLUS,
    AUTO_BUY_MIN_DISCOUNT_CHEAP,
    AUTO_BUY_MIN_DISCOUNT_EXPENSIVE,
    RARE_MODEL_DEFAULT_BALANCE_TON,
    RARE_MODEL_DEFAULT_ENABLED,
    RARE_MODEL_MAX_PREMIUM_10_PLUS_PERCENT,
    RARE_MODEL_MAX_PREMIUM_5_TO_10_PERCENT,
    RARE_MODEL_MAX_RARITY_PERCENT,
    RARE_MODEL_MIN_LISTING_PRICE_TON,
    RARE_MODEL_PRICE_THRESHOLD_TON,
    MAIN_SNIPER_FORCE_RUNNING_ON_START,
    FLOOR_REFRESH_SECONDS,
    FLOOR_REFRESH_MIN_SECONDS,
    AUTO_BUY_ZERO_RARITY_MIN_DISCOUNT_PERCENT,
)

logger = logging.getLogger("mrkt.state")

STATE_FILE = Path(__file__).resolve().with_name("runtime_state.json")

MUTABLE_FIELDS = (
    # Main sniper
    "is_running",
    "balance",
    "threshold",
    "discount_cheap",
    "discount_expensive",
    "discount_10_20",
    "discount_20_plus",
    "buy_depth_guard_enabled",
    "buy_depth_guard_max_at_or_below",
    "buy_depth_guard_near_price_percent",
    "buy_depth_guard_scan_count",
    # Main sniper stats
    "stat_missed_bought_before",
    "stat_missed_balance_high_value",
    "stat_missed_balance_8_20",
    "stat_missed_balance_20_50",
    "stat_missed_balance_50_plus",
    "stat_missed_balance_best_day_date",
    "stat_missed_balance_best_day_discount",
    "stat_missed_balance_best_day_price",
    "stat_missed_balance_best_day_floor",
    "stat_missed_balance_best_day_collection",
    "stat_missed_balance_best_day_gift_id",
    # Rare-model sniper
    "rare_enabled",
    "rare_balance",
    "rare_threshold",
    "rare_min_listing_price",
    "rare_max_rarity_percent",
    "rare_premium_5_10",
    "rare_premium_10_plus",
    # Auto-sell mode
    "sell_enabled",
    "sell_balance",
    "sell_price_threshold",
    "sell_target_profit_5_10",
    "sell_target_profit_10_plus",
    "sell_floor_premium_5_10",
    "sell_floor_premium_10_plus",
    "sell_relist_interval_seconds",
    "sell_stuck_hours",
    "sell_prompt_price_threshold",
    "sell_loss_cap_percent",
    "sell_rare_protect_percent",
    "sell_order_mode_critical_only",
    # Reliability improvements
    "floor_refresh_seconds",
    "zero_rarity_min_discount",
    "zero_rarity_gate_enabled",
)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


class AppState:
    def __init__(self) -> None:
        object.__setattr__(self, "_state_file", STATE_FILE)
        object.__setattr__(self, "_autosave_enabled", False)

        defaults = self._defaults()
        for key, value in defaults.items():
            object.__setattr__(self, key, value)

        persisted = self._load()
        for key, value in persisted.items():
            object.__setattr__(self, key, value)

        # Safe startup: do not allow persisted pause state to brick scanning after restart.
        if MAIN_SNIPER_FORCE_RUNNING_ON_START:
            object.__setattr__(self, "is_running", True)

        # User requirement: rare-model mode must always be OFF after each start.
        object.__setattr__(self, "rare_enabled", False)
        # Same safe startup behavior for auto-sell mode.
        object.__setattr__(self, "sell_enabled", False)

        object.__setattr__(self, "_autosave_enabled", True)
        self.save()

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if getattr(self, "_autosave_enabled", False) and name in MUTABLE_FIELDS:
            self.save()

    def _defaults(self) -> dict[str, Any]:
        return {
            # Main sniper
            "is_running": True,
            "balance": float(AUTO_BUY_MAX_PRICE_TON),
            "threshold": float(AUTO_BUY_DISCOUNT_THRESHOLD_PRICE_TON),  # 4.0
            "discount_cheap": float(AUTO_BUY_MIN_DISCOUNT_CHEAP),  # <4
            "discount_expensive": float(AUTO_BUY_MIN_DISCOUNT_EXPENSIVE),  # >=4 (legacy)
            "discount_10_20": float(AUTO_BUY_MIN_DISCOUNT_10_TO_20),
            "discount_20_plus": float(AUTO_BUY_MIN_DISCOUNT_20_PLUS),
            "buy_depth_guard_enabled": bool(AUTO_BUY_DEPTH_GUARD_DEFAULT_ENABLED),
            "buy_depth_guard_max_at_or_below": int(AUTO_BUY_DEPTH_GUARD_MAX_AT_OR_BELOW),
            "buy_depth_guard_near_price_percent": float(AUTO_BUY_DEPTH_GUARD_NEAR_PRICE_PERCENT),
            "buy_depth_guard_scan_count": int(AUTO_BUY_DEPTH_GUARD_SCAN_COUNT),
            # Main sniper stats
            "stat_missed_bought_before": 0,
            "stat_missed_balance_high_value": 0,
            "stat_missed_balance_8_20": 0,
            "stat_missed_balance_20_50": 0,
            "stat_missed_balance_50_plus": 0,
            "stat_missed_balance_best_day_date": "",
            "stat_missed_balance_best_day_discount": 0.0,
            "stat_missed_balance_best_day_price": 0.0,
            "stat_missed_balance_best_day_floor": 0.0,
            "stat_missed_balance_best_day_collection": "",
            "stat_missed_balance_best_day_gift_id": "",
            # Rare-model sniper
            "rare_enabled": bool(RARE_MODEL_DEFAULT_ENABLED),
            "rare_balance": float(RARE_MODEL_DEFAULT_BALANCE_TON),
            "rare_threshold": float(RARE_MODEL_PRICE_THRESHOLD_TON),  # 10.0
            "rare_min_listing_price": float(RARE_MODEL_MIN_LISTING_PRICE_TON),  # 5.0
            "rare_max_rarity_percent": float(RARE_MODEL_MAX_RARITY_PERCENT),  # 1.0
            "rare_premium_5_10": float(RARE_MODEL_MAX_PREMIUM_5_TO_10_PERCENT),
            "rare_premium_10_plus": float(RARE_MODEL_MAX_PREMIUM_10_PLUS_PERCENT),
            # Auto-sell mode
            "sell_enabled": bool(AUTO_SELL_DEFAULT_ENABLED),
            "sell_balance": float(AUTO_SELL_DEFAULT_BALANCE_TON),
            "sell_price_threshold": float(AUTO_SELL_PRICE_THRESHOLD_TON),
            "sell_target_profit_5_10": float(AUTO_SELL_TARGET_PROFIT_5_10_PERCENT),
            "sell_target_profit_10_plus": float(AUTO_SELL_TARGET_PROFIT_10_PLUS_PERCENT),
            "sell_floor_premium_5_10": float(AUTO_SELL_FLOOR_PREMIUM_5_10_PERCENT),
            "sell_floor_premium_10_plus": float(AUTO_SELL_FLOOR_PREMIUM_10_PLUS_PERCENT),
            "sell_relist_interval_seconds": float(AUTO_SELL_RELIST_INTERVAL_SECONDS),
            "sell_stuck_hours": float(AUTO_SELL_STUCK_HOURS),
            "sell_prompt_price_threshold": float(AUTO_SELL_PROMPT_PRICE_THRESHOLD_TON),
            "sell_loss_cap_percent": float(AUTO_SELL_LOSS_CAP_PERCENT),
            "sell_rare_protect_percent": float(AUTO_SELL_RARE_PROTECT_PERCENT),
            "sell_order_mode_critical_only": bool(AUTO_SELL_ORDER_MODE_CRITICAL_ONLY),
            # Reliability improvements
            "floor_refresh_seconds": float(FLOOR_REFRESH_SECONDS),
            "zero_rarity_min_discount": float(AUTO_BUY_ZERO_RARITY_MIN_DISCOUNT_PERCENT),
            "zero_rarity_gate_enabled": True,
        }

    def _load(self) -> dict[str, Any]:
        if not self._state_file.exists():
            return {}

        try:
            raw_data = self._state_file.read_text(encoding="utf-8")
            data = json.loads(raw_data)
        except Exception as exc:
            logger.warning("Failed to load runtime state, using defaults: %s", exc)
            return {}

        if not isinstance(data, dict):
            logger.warning("Invalid runtime state format, using defaults.")
            return {}

        defaults = self._defaults()
        return {
            # Main sniper
            "is_running": _as_bool(data.get("is_running"), defaults["is_running"]),
            "balance": max(0.0, _as_float(data.get("balance"), defaults["balance"])),
            "threshold": max(
                0.0, _as_float(data.get("threshold"), defaults["threshold"])
            ),
            "discount_cheap": _as_float(
                data.get("discount_cheap"), defaults["discount_cheap"]
            ),
            "discount_expensive": _as_float(
                data.get("discount_expensive"), defaults["discount_expensive"]
            ),
            "discount_10_20": _as_float(
                data.get("discount_10_20"), defaults["discount_10_20"]
            ),
            "discount_20_plus": _as_float(
                data.get("discount_20_plus"), defaults["discount_20_plus"]
            ),
            "buy_depth_guard_enabled": _as_bool(
                data.get("buy_depth_guard_enabled"),
                defaults["buy_depth_guard_enabled"],
            ),
            "buy_depth_guard_max_at_or_below": max(
                1,
                min(
                    20,
                    _as_int(
                        data.get("buy_depth_guard_max_at_or_below"),
                        defaults["buy_depth_guard_max_at_or_below"],
                    ),
                ),
            ),
            "buy_depth_guard_near_price_percent": max(
                0.0,
                min(
                    5.0,
                    _as_float(
                        data.get("buy_depth_guard_near_price_percent"),
                        defaults["buy_depth_guard_near_price_percent"],
                    ),
                ),
            ),
            "buy_depth_guard_scan_count": max(
                10,
                min(
                    100,
                    _as_int(
                        data.get("buy_depth_guard_scan_count"),
                        defaults["buy_depth_guard_scan_count"],
                    ),
                ),
            ),
            "stat_missed_bought_before": max(
                0,
                _as_int(
                    data.get("stat_missed_bought_before"),
                    defaults["stat_missed_bought_before"],
                ),
            ),
            "stat_missed_balance_high_value": max(
                0,
                _as_int(
                    data.get("stat_missed_balance_high_value"),
                    defaults["stat_missed_balance_high_value"],
                ),
            ),
            "stat_missed_balance_8_20": max(
                0,
                _as_int(
                    data.get("stat_missed_balance_8_20"),
                    defaults["stat_missed_balance_8_20"],
                ),
            ),
            "stat_missed_balance_20_50": max(
                0,
                _as_int(
                    data.get("stat_missed_balance_20_50"),
                    defaults["stat_missed_balance_20_50"],
                ),
            ),
            "stat_missed_balance_50_plus": max(
                0,
                _as_int(
                    data.get("stat_missed_balance_50_plus"),
                    defaults["stat_missed_balance_50_plus"],
                ),
            ),
            "stat_missed_balance_best_day_date": _as_str(
                data.get("stat_missed_balance_best_day_date"),
                defaults["stat_missed_balance_best_day_date"],
            ),
            "stat_missed_balance_best_day_discount": max(
                0.0,
                _as_float(
                    data.get("stat_missed_balance_best_day_discount"),
                    defaults["stat_missed_balance_best_day_discount"],
                ),
            ),
            "stat_missed_balance_best_day_price": max(
                0.0,
                _as_float(
                    data.get("stat_missed_balance_best_day_price"),
                    defaults["stat_missed_balance_best_day_price"],
                ),
            ),
            "stat_missed_balance_best_day_floor": max(
                0.0,
                _as_float(
                    data.get("stat_missed_balance_best_day_floor"),
                    defaults["stat_missed_balance_best_day_floor"],
                ),
            ),
            "stat_missed_balance_best_day_collection": _as_str(
                data.get("stat_missed_balance_best_day_collection"),
                defaults["stat_missed_balance_best_day_collection"],
            ),
            "stat_missed_balance_best_day_gift_id": _as_str(
                data.get("stat_missed_balance_best_day_gift_id"),
                defaults["stat_missed_balance_best_day_gift_id"],
            ),
            # Rare-model sniper
            "rare_enabled": _as_bool(data.get("rare_enabled"), defaults["rare_enabled"]),
            "rare_balance": max(
                0.0, _as_float(data.get("rare_balance"), defaults["rare_balance"])
            ),
            "rare_threshold": max(
                0.0, _as_float(data.get("rare_threshold"), defaults["rare_threshold"])
            ),
            "rare_min_listing_price": max(
                0.0,
                _as_float(
                    data.get("rare_min_listing_price"),
                    defaults["rare_min_listing_price"],
                ),
            ),
            "rare_max_rarity_percent": max(
                0.0,
                _as_float(
                    data.get("rare_max_rarity_percent"),
                    defaults["rare_max_rarity_percent"],
                ),
            ),
            "rare_premium_5_10": max(
                0.0,
                _as_float(data.get("rare_premium_5_10"), defaults["rare_premium_5_10"]),
            ),
            "rare_premium_10_plus": max(
                0.0,
                _as_float(
                    data.get("rare_premium_10_plus"), defaults["rare_premium_10_plus"]
                ),
            ),
            # Auto-sell mode
            "sell_enabled": _as_bool(data.get("sell_enabled"), defaults["sell_enabled"]),
            "sell_balance": max(
                0.0, _as_float(data.get("sell_balance"), defaults["sell_balance"])
            ),
            "sell_price_threshold": max(
                0.0,
                _as_float(
                    data.get("sell_price_threshold"),
                    defaults["sell_price_threshold"],
                ),
            ),
            "sell_target_profit_5_10": max(
                0.0,
                _as_float(
                    data.get("sell_target_profit_5_10"),
                    defaults["sell_target_profit_5_10"],
                ),
            ),
            "sell_target_profit_10_plus": max(
                0.0,
                _as_float(
                    data.get("sell_target_profit_10_plus"),
                    defaults["sell_target_profit_10_plus"],
                ),
            ),
            "sell_floor_premium_5_10": max(
                0.0,
                _as_float(
                    data.get("sell_floor_premium_5_10"),
                    defaults["sell_floor_premium_5_10"],
                ),
            ),
            "sell_floor_premium_10_plus": max(
                0.0,
                _as_float(
                    data.get("sell_floor_premium_10_plus"),
                    defaults["sell_floor_premium_10_plus"],
                ),
            ),
            "sell_relist_interval_seconds": max(
                30.0,
                _as_float(
                    data.get("sell_relist_interval_seconds"),
                    defaults["sell_relist_interval_seconds"],
                ),
            ),
            "sell_stuck_hours": max(
                0.1,
                _as_float(data.get("sell_stuck_hours"), defaults["sell_stuck_hours"]),
            ),
            "sell_prompt_price_threshold": max(
                0.0,
                _as_float(
                    data.get("sell_prompt_price_threshold"),
                    defaults["sell_prompt_price_threshold"],
                ),
            ),
            "sell_loss_cap_percent": max(
                0.0,
                _as_float(
                    data.get("sell_loss_cap_percent"),
                    defaults["sell_loss_cap_percent"],
                ),
            ),
            "sell_rare_protect_percent": max(
                0.0,
                _as_float(
                    data.get("sell_rare_protect_percent"),
                    defaults["sell_rare_protect_percent"],
                ),
            ),
            "sell_order_mode_critical_only": _as_bool(
                data.get("sell_order_mode_critical_only"),
                defaults["sell_order_mode_critical_only"],
            ),
            # Reliability improvements
            "floor_refresh_seconds": max(
                float(FLOOR_REFRESH_MIN_SECONDS),
                _as_float(
                    data.get("floor_refresh_seconds"),
                    defaults["floor_refresh_seconds"],
                ),
            ),
            "zero_rarity_min_discount": max(
                0.0,
                min(
                    100.0,
                    _as_float(
                        data.get("zero_rarity_min_discount"),
                        defaults["zero_rarity_min_discount"],
                    ),
                ),
            ),
            "zero_rarity_gate_enabled": _as_bool(
                data.get("zero_rarity_gate_enabled"),
                defaults["zero_rarity_gate_enabled"],
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in MUTABLE_FIELDS}

    def save(self) -> None:
        payload = self.as_dict()
        tmp_file = self._state_file.with_suffix(".json.tmp")
        try:
            tmp_file.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self._state_file)
        except Exception:
            logger.exception("Failed to persist runtime state to %s", self._state_file)


state = AppState()
