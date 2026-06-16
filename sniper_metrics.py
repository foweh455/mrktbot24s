from __future__ import annotations

from typing import Any

from config import (
    STATS_MISSED_BALANCE_MIN_DISCOUNT_PERCENT,
    STATS_MISSED_BALANCE_MIN_PRICE_TON,
)


BALANCE_MISS_BUCKET_8_20 = "8_20"
BALANCE_MISS_BUCKET_20_50 = "20_50"
BALANCE_MISS_BUCKET_50_PLUS = "50_plus"


def is_already_bought_response(res: object) -> bool:
    """
    Detect responses meaning the lot was already bought by someone else.
    """
    if isinstance(res, list):
        return len(res) == 0

    if isinstance(res, dict):
        raw = " ".join(
            str(part)
            for part in (
                res.get("error"),
                res.get("title"),
                res.get("message"),
                res.get("errors"),
            )
            if part is not None
        ).lower()
        sold_markers = (
            "already sold",
            "уже продан",
            "sold",
            "not available",
            "not found",
        )
        return any(marker in raw for marker in sold_markers)

    return False


def should_count_balance_miss(
    *,
    price_ton: float,
    discount_percent: float,
    balance_limit_ton: float,
) -> bool:
    """
    Count stat only when:
    - lot price is above current balance limit;
    - lot is expensive enough (> configured threshold);
    - lot is below floor by enough percent (>= configured threshold).
    """
    return (
        price_ton > balance_limit_ton
        and price_ton > float(STATS_MISSED_BALANCE_MIN_PRICE_TON)
        and discount_percent >= float(STATS_MISSED_BALANCE_MIN_DISCOUNT_PERCENT)
    )


def get_balance_miss_bucket(price_ton: float) -> str:
    """
    Price range buckets for high-value missed-by-balance stats.
    """
    if price_ton >= 50.0:
        return BALANCE_MISS_BUCKET_50_PLUS
    if price_ton >= 20.0:
        return BALANCE_MISS_BUCKET_20_50
    return BALANCE_MISS_BUCKET_8_20


def build_listing_key(gift_id: str, listing_price_nano: int) -> tuple[str, int]:
    return (str(gift_id), int(listing_price_nano))
