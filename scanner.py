"""
MRKT NFT Gift Sniper Bot вЂ” Scanner
Monitors collections for new listings and highlights below-floor deals.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from api import MRKTClient
from config import (
    MIN_DISCOUNT_PERCENT,
    MIN_FLOOR_PRICE_TON,
    WHITELIST_COLLECTIONS,
    BLACKLIST_COLLECTIONS,
    MAX_CONCURRENT_REQUESTS,
    LISTINGS_PER_SCAN,
    AUTO_BUY_SKIP_TRADE_BAN,
    AUTO_BUY_TRADE_BAN_DISCOUNT_REQUIRED_FROM_DAYS,
    AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS,
    AUTO_BUY_TRADE_BAN_MIN_DISCOUNT_PERCENT,
)

logger = logging.getLogger("mrkt.scanner")
SECONDS_PER_DAY = 24 * 60 * 60


def nano_to_ton(nano: int) -> float:
    """Convert nanoTON to TON."""
    return nano / 1_000_000_000


def parse_per_mille(value: Any) -> int | None:
    """Convert rarity value to per-mille integer (or None)."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def parse_api_datetime(value: Any) -> datetime | None:
    """Parse MRKT datetime values like '2026-04-16T02:23:53.265368Z'."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith("0001-01-01"):
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def analyze_trade_lock(
    gift: dict[str, Any],
    *,
    discount_percent: float = 0.0,
    now_ts: int | None = None,
) -> tuple[bool, int | None, int, str]:
    """
    Returns:
        (blocked, lock_until_ts, lock_seconds, lock_source_field)
    """
    current_ts = int(now_ts or time.time())
    if not AUTO_BUY_SKIP_TRADE_BAN:
        return False, None, 0, ""

    lock_fields = (
        "nextResaleDate",
        "nextTransferDate",
        "unlockDate",
        "waitGiftUntil",
        "exportDate",
        "returnLockedUntil",
        "validateRegularGiftAt",
    )
    future_locks: list[tuple[str, int]] = []
    for field_name in lock_fields:
        dt = parse_api_datetime(gift.get(field_name))
        if dt is None:
            continue
        ts = int(dt.timestamp())
        if ts > current_ts:
            future_locks.append((field_name, ts))

    if not future_locks:
        return False, None, 0, ""

    # Be conservative: use the farthest lock.
    lock_source, lock_until_ts = max(future_locks, key=lambda x: x[1])
    lock_seconds = lock_until_ts - current_ts
    lock_days = max(0.0, lock_seconds / SECONDS_PER_DAY)
    discount_gate_days = max(0.0, float(AUTO_BUY_TRADE_BAN_DISCOUNT_REQUIRED_FROM_DAYS))
    hard_skip_days = max(discount_gate_days, float(AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS))
    min_discount = float(AUTO_BUY_TRADE_BAN_MIN_DISCOUNT_PERCENT)

    if lock_days >= hard_skip_days:
        blocked = True
    elif lock_days >= discount_gate_days:
        blocked = discount_percent < min_discount
    else:
        blocked = False

    return blocked, lock_until_ts, lock_seconds, lock_source


@dataclass
class Listing:
    """Represents a gift listing on the marketplace."""

    gift_id: str
    collection_name: str
    collection_title: str
    listing_price_nano: int
    floor_price_nano: int
    discount_percent: float  # positive = below floor, negative = above floor
    model_name: str = ""
    model_rarity_per_mille: int | None = None
    backdrop_name: str = ""
    number: int | None = None
    is_below_floor: bool = False
    trade_lock_blocked: bool = False
    trade_lock_until_ts: int | None = None
    trade_lock_seconds: int = 0
    trade_lock_source: str = ""

    @property
    def listing_price_ton(self) -> float:
        return nano_to_ton(self.listing_price_nano)

    @property
    def floor_price_ton(self) -> float:
        return nano_to_ton(self.floor_price_nano)

    @property
    def model_rarity_percent(self) -> float | None:
        if self.model_rarity_per_mille is None:
            return None
        return self.model_rarity_per_mille / 10.0


def is_zero_rarity(listing: "Listing") -> bool:
    """Return True if listing has no meaningful model/rarity data.

    Conservative OR-rule:
        - model_rarity_per_mille is None or 0
        - model_name is empty or whitespace-only
    """
    per_mille = listing.model_rarity_per_mille
    if per_mille is None or per_mille == 0:
        return True
    name = (listing.model_name or "").strip()
    if not name:
        return True
    return False


class Scanner:
    """Scans the MRKT marketplace for new listings."""

    def __init__(self, client: MRKTClient) -> None:
        self.client = client
        self._seen_ids: set[tuple[str, int]] = set()  # track (gift_id, priceNanoTons)
        self._floor_prices: dict[str, int] = {}  # name -> floorPriceNanoTons
        self._collection_titles: dict[str, str] = {}  # name -> title

    async def refresh_floor_prices(self) -> dict[str, int]:
        """Fetch fresh floor prices for all collections.

        Устойчив к сбоям: при исключении или пустом ответе от
        ``get_collections()`` сохраняет ранее известные значения
        ``_floor_prices`` и ``_collection_titles`` и возвращает их без
        изменений. Успешное обновление дополнительно логируется на уровне
        INFO (каденс/elapsed) и, при изменениях, на уровне DEBUG
        (до 10 пар ``(name, old_ton, new_ton)``).
        """
        previous_floors = dict(self._floor_prices)
        previous_titles = dict(self._collection_titles)
        start_ts = time.monotonic()

        try:
            collections = await self.client.get_collections()
        except Exception as exc:
            logger.warning(
                "Floor refresh failed, keeping previous floors: %s", exc
            )
            self._floor_prices = previous_floors
            self._collection_titles = previous_titles
            return self._floor_prices

        if not collections:
            logger.warning(
                "Floor refresh returned empty collection list, keeping previous floors"
            )
            self._floor_prices = previous_floors
            self._collection_titles = previous_titles
            return self._floor_prices

        self._floor_prices.clear()
        self._collection_titles.clear()

        for col in collections:
            name = col.get("name", "")
            title = col.get("title", name)
            floor = col.get("floorPriceNanoTons")

            if not name or not floor:
                continue

            if col.get("isHidden", False):
                continue

            if WHITELIST_COLLECTIONS and title not in WHITELIST_COLLECTIONS and name not in WHITELIST_COLLECTIONS:
                continue

            if title in BLACKLIST_COLLECTIONS or name in BLACKLIST_COLLECTIONS:
                continue

            if MIN_FLOOR_PRICE_TON > 0 and nano_to_ton(floor) < MIN_FLOOR_PRICE_TON:
                continue

            self._floor_prices[name] = floor
            self._collection_titles[name] = title

        changed = sum(
            1
            for name, new_floor in self._floor_prices.items()
            if previous_floors.get(name) != new_floor
        )
        elapsed = time.monotonic() - start_ts

        logger.info(f"Loaded floor prices for {len(self._floor_prices)} collections")
        logger.info(
            "Floor refresh: %d collections, %d floors changed, elapsed=%.2fs",
            len(self._floor_prices),
            changed,
            elapsed,
        )

        if changed > 0:
            diffs: list[tuple[str, float, float]] = []
            for name, new_floor in self._floor_prices.items():
                old_floor = previous_floors.get(name)
                if old_floor == new_floor:
                    continue
                old_ton = nano_to_ton(old_floor) if old_floor else 0.0
                new_ton = nano_to_ton(new_floor)
                diffs.append((name, old_ton, new_ton))
                if len(diffs) >= 10:
                    break
            logger.debug("Floor changes (up to 10): %s", diffs)

        return self._floor_prices

    async def _scan_collection(self, collection_name: str) -> list[Listing]:
        """Scan a single collection for new listings."""
        floor_price = self._floor_prices.get(collection_name, 0)

        # Fetch cheapest listings for this collection
        data = await self.client.get_listings(
            collection_names=[collection_name],
            low_to_high=True,
            count=LISTINGS_PER_SCAN,
        )

        listings: list[Listing] = []
        gifts = data.get("gifts", [])
        now_ts = int(time.time())

        for gift in gifts:
            price = gift.get("salePrice")
            if not price:
                continue

            price = int(price)
            gift_id = gift.get("id", "")

            # Skip if we already saw THIS EXACT LISTING at THIS EXACT PRICE
            if (gift_id, price) in self._seen_ids:
                continue

            # Calculate discount (positive = below floor = GOOD)
            if floor_price > 0:
                discount = ((floor_price - price) / floor_price) * 100
            else:
                discount = 0.0

            # Alert if discount is at least the configured minimum
            is_below = discount >= MIN_DISCOUNT_PERCENT
            (
                trade_lock_blocked,
                trade_lock_until_ts,
                trade_lock_seconds,
                trade_lock_source,
            ) = analyze_trade_lock(
                gift,
                discount_percent=discount,
                now_ts=now_ts,
            )

            listing = Listing(
                gift_id=gift_id,
                collection_name=collection_name,
                collection_title=self._collection_titles.get(
                    collection_name, collection_name
                ),
                listing_price_nano=price,
                floor_price_nano=floor_price,
                discount_percent=discount,
                model_name=gift.get("modelName", ""),
                model_rarity_per_mille=parse_per_mille(gift.get("modelRarityPerMille")),
                backdrop_name=gift.get("backdropName", ""),
                number=gift.get("number"),
                is_below_floor=is_below,
                trade_lock_blocked=trade_lock_blocked,
                trade_lock_until_ts=trade_lock_until_ts,
                trade_lock_seconds=trade_lock_seconds,
                trade_lock_source=trade_lock_source,
            )
            listings.append(listing)

        return listings

    async def scan_all(self) -> tuple[list[Listing], list[Listing]]:
        """
        Scan globally for new listings.
        Returns (below_floor_deals, all_new_listings).
        Only returns listings not previously seen.
        """
        if not self._floor_prices:
            await self.refresh_floor_prices()

        all_new: list[Listing] = []
        below_floor: list[Listing] = []

        try:
            # 1 РіР»РѕР±Р°Р»СЊРЅС‹Р№ Р·Р°РїСЂРѕСЃ РІРјРµСЃС‚Рѕ РєСѓС‡Рё РјРµР»РєРёС… (ordering=None РІРµСЂРЅРµС‚ Р»РµРЅС‚Сѓ РёР· 100 СЃР°РјС‹С… РЅРѕРІС‹С… Р»РѕС‚РѕРІ РЅР° СЂС‹РЅРєРµ)
            data = await self.client.get_listings(
                collection_names=[],  # РїСѓСЃС‚РѕР№ СЃРїРёСЃРѕРє РѕР·РЅР°С‡Р°РµС‚ РїРѕРёСЃРє РїРѕ РІСЃРµРј РєРѕР»Р»РµРєС†РёСЏРј
                low_to_high=False,
                count=100,
                ordering="None",
            )
        except Exception as e:
            if "401" in str(e) or "Unauthorized" in str(e):
                raise
            logger.error(f"Error scanning global feed: {e}")
            return [], []

        gifts = data.get("gifts", [])
        now_ts = int(time.time())

        for gift in gifts:
            price = gift.get("salePrice")
            collection_name = gift.get("collectionName", gift.get("title"))
            if not price or not collection_name:
                continue

            # РРіРЅРѕСЂРёСЂСѓРµРј РєРѕР»Р»РµРєС†РёРё, РєРѕС‚РѕСЂС‹Рµ РЅРµ РѕС‚СЃР»РµР¶РёРІР°СЋС‚СЃСЏ
            if collection_name not in self._floor_prices:
                continue

            price = int(price)
            gift_id = gift.get("id", "")

            # РџСЂРѕРІРµСЂСЏРµРј, РІРёРґРµР»Рё Р»Рё СѓР¶Рµ СЌС‚РѕС‚ РїРѕРґР°СЂРѕРє РїРѕ СЌС‚РѕР№ С†РµРЅРµ
            if (gift_id, price) in self._seen_ids:
                continue

            floor_price = self._floor_prices[collection_name]

            if floor_price > 0:
                discount = ((floor_price - price) / floor_price) * 100
            else:
                discount = 0.0

            is_below = discount >= MIN_DISCOUNT_PERCENT
            (
                trade_lock_blocked,
                trade_lock_until_ts,
                trade_lock_seconds,
                trade_lock_source,
            ) = analyze_trade_lock(
                gift,
                discount_percent=discount,
                now_ts=now_ts,
            )

            listing = Listing(
                gift_id=gift_id,
                collection_name=collection_name,
                collection_title=self._collection_titles.get(collection_name, collection_name),
                listing_price_nano=price,
                floor_price_nano=floor_price,
                discount_percent=discount,
                model_name=gift.get("modelName", ""),
                model_rarity_per_mille=parse_per_mille(gift.get("modelRarityPerMille")),
                backdrop_name=gift.get("backdropName", ""),
                number=gift.get("number"),
                is_below_floor=is_below,
                trade_lock_blocked=trade_lock_blocked,
                trade_lock_until_ts=trade_lock_until_ts,
                trade_lock_seconds=trade_lock_seconds,
                trade_lock_source=trade_lock_source,
            )

            # Mark as seen
            self._seen_ids.add((listing.gift_id, listing.listing_price_nano))
            all_new.append(listing)

            if listing.is_below_floor:
                below_floor.append(listing)

        # Sort: best deals first
        all_new.sort(key=lambda x: x.listing_price_nano)
        below_floor.sort(key=lambda x: x.discount_percent, reverse=True)

        return below_floor, all_new

    def clear_seen(self) -> None:
        """Clear the seen listing cache."""
        self._seen_ids.clear()

