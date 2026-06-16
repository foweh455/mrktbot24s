"""
Separate rare-model strategy.
It can be toggled independently from the regular below-floor logic.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from scanner import Listing

logger = logging.getLogger("mrkt.rare_model_strategy")


@dataclass
class RareModelSettings:
    enabled: bool
    max_rarity_percent: float
    min_listing_price_ton: float
    threshold_price_ton: float
    max_premium_5_10_percent: float
    max_premium_10_plus_percent: float
    below_floor_force_buy: bool


@dataclass
class RareModelCandidate:
    listing: Listing
    rarity_percent: float
    premium_percent: float
    premium_limit_percent: float


def _premium_limit_for(listing_price_ton: float, settings: RareModelSettings) -> float:
    if listing_price_ton >= settings.threshold_price_ton:
        return settings.max_premium_10_plus_percent
    return settings.max_premium_5_10_percent


def _is_base_match(listing: Listing, settings: RareModelSettings) -> bool:
    if not settings.enabled:
        return False

    # Never buy long trade-ban gifts in rare strategy.
    if listing.trade_lock_blocked:
        return False

    rarity_percent = listing.model_rarity_percent
    if rarity_percent is None:
        return False

    if rarity_percent > settings.max_rarity_percent:
        return False

    if listing.listing_price_ton < settings.min_listing_price_ton:
        return False

    if listing.floor_price_ton <= 0:
        return False

    return True


def is_rare_model_below_floor_signal(
    listing: Listing,
    settings: RareModelSettings,
) -> bool:
    """
    Rare model and already below collection floor.
    Used for force-buy path (ignores regular discount thresholds).
    """
    return (
        settings.below_floor_force_buy
        and _is_base_match(listing, settings)
        and listing.discount_percent > 0
    )


def _build_candidate(
    listing: Listing,
    settings: RareModelSettings,
) -> RareModelCandidate | None:
    if not _is_base_match(listing, settings):
        return None

    # discount_percent > 0 means below floor, < 0 means above floor
    if listing.discount_percent >= 0:
        return None

    premium_percent = -listing.discount_percent
    premium_limit_percent = _premium_limit_for(listing.listing_price_ton, settings)
    if premium_percent > premium_limit_percent:
        return None

    return RareModelCandidate(
        listing=listing,
        rarity_percent=listing.model_rarity_percent or 0.0,
        premium_percent=premium_percent,
        premium_limit_percent=premium_limit_percent,
    )


def select_rare_model_candidates(
    all_new_listings: list[Listing],
    settings: RareModelSettings,
    excluded_gift_ids: set[str] | None = None,
) -> list[RareModelCandidate]:
    """
    Select rare models above floor that still fit allowed premium.
    """
    if not settings.enabled:
        return []

    excluded = excluded_gift_ids or set()
    selected: list[RareModelCandidate] = []

    for listing in all_new_listings:
        if listing.gift_id in excluded:
            continue
        candidate = _build_candidate(listing, settings)
        if candidate is not None:
            selected.append(candidate)

    selected.sort(
        key=lambda c: (
            c.rarity_percent,
            c.premium_percent,
            c.listing.listing_price_ton,
        )
    )

    if selected:
        logger.info(
            "Rare-model candidates: %s (best rarity %.2f%%, best premium %.2f%%)",
            len(selected),
            selected[0].rarity_percent,
            selected[0].premium_percent,
        )

    return selected
