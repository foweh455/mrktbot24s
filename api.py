import logging
import asyncio
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import aiohttp
from aiohttp.client_exceptions import ClientResponseError

import config
from authenticator import fetch_new_mrkt_token
from config import API_BASE_URL, HEADERS, COOKIES, MAX_CONCURRENT_REQUESTS, LISTINGS_PER_SCAN

logger = logging.getLogger("mrkt.api")


def ton_to_nano(value_ton: float) -> int:
    return int(round(float(value_ton) * 1_000_000_000))


def normalize_market_price_ton(value_ton: float) -> float:
    """
    Normalize listing price to market tick size (0.01 TON).
    Prevents "Invalid price value" on endpoints that reject over-precision.
    """
    try:
        parsed = Decimal(str(float(value_ton)))
    except (TypeError, ValueError):
        parsed = Decimal("0")
    normalized = parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if normalized < Decimal("0.01"):
        normalized = Decimal("0.01")
    return float(normalized)


class MRKTClient:
    """Async client for interacting with the MRKT API."""

    def __init__(self) -> None:
        self.base_url = API_BASE_URL
        self.headers = HEADERS.copy()
        self.cookies = COOKIES.copy()
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._token_raw: str = ""
        self._use_bearer_auth = str(self.headers.get("Authorization", "")).strip().lower().startswith(
            "bearer "
        )
        self._apply_token(
            str(
                config.API_TOKEN
                or self.cookies.get("access_token")
                or self.headers.get("Authorization")
                or ""
            ).strip(),
            prefer_bearer=self._use_bearer_auth,
        )
        
        # Rate limiting state to avoid 429 / 502 from Cloudflare
        self._global_pause_until = 0.0
        self._last_request_time = 0.0
        self._request_lock = asyncio.Lock()

    def _apply_token(self, token: str, *, prefer_bearer: bool | None = None) -> bool:
        """
        Keep auth data in sync across runtime headers/cookies/config.
        """
        token_value = str(token or "").strip()
        if not token_value:
            return False

        raw_token = token_value
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token[7:].strip()
        if not raw_token:
            return False

        use_bearer = self._use_bearer_auth if prefer_bearer is None else bool(prefer_bearer)
        auth_header = f"Bearer {raw_token}" if use_bearer else raw_token

        self._token_raw = raw_token
        self._use_bearer_auth = use_bearer
        self.headers["Authorization"] = auth_header
        self.cookies["access_token"] = raw_token

        # Keep module-level config values aligned for any direct users.
        config.API_TOKEN = raw_token
        config.HEADERS["Authorization"] = auth_header
        config.COOKIES["access_token"] = raw_token
        return True

    async def _recreate_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _toggle_authorization_format(self) -> bool:
        if not self._token_raw:
            return False
        toggled = not self._use_bearer_auth
        if not self._apply_token(self._token_raw, prefer_bearer=toggled):
            return False
        await self._recreate_session()
        logger.warning(
            "Switched Authorization format to %s and recreated session.",
            "Bearer" if toggled else "raw token",
        )
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                cookies=self.cookies,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def _update_token(self) -> bool:
        """Attempt to fetch a new token and update session headers."""
        new_token = await fetch_new_mrkt_token()
        if new_token:
            logger.info("Successfully fetched new token via Telegram auto-auth!")
            if not self._apply_token(new_token):
                logger.error("Fetched token is empty after normalization.")
                return False
            await self._recreate_session()
            return True
        return False

    async def _request(
        self, method: str, endpoint: str, max_retries: int = 3, **kwargs: Any
    ) -> dict[str, Any]:
        """Make an HTTP request with retry logic and auto-auth."""
        url = f"{self.base_url}{endpoint}"
        refreshed_after_401 = False
        switched_auth_format = False
        
        for attempt in range(1, max_retries + 1):
            async with self._semaphore:
                # 1. Global pause (if we hit a 429/50x recently)
                now = time.monotonic()
                if now < self._global_pause_until:
                    await asyncio.sleep(self._global_pause_until - now)

                session = await self._get_session()
                try:
                    async with session.request(method, url, **kwargs) as resp:
                        if not resp.ok:
                            error_text = await resp.text()
                            logger.error(f"HTTP error {resp.status} on {url}: {error_text}")
                            
                            # Global backoff for 429 Too Many Requests or 502/504 Bad Gateway
                            if resp.status == 429 or resp.status >= 500:
                                backoff_time = 3.0 * attempt
                                self._global_pause_until = time.monotonic() + backoff_time
                                logger.warning(f"Global API pause for {backoff_time}s due to error {resp.status}")

                            if resp.status == 401:
                                logger.error("401 Unauthorized on attempt %s", attempt)
                                if attempt < max_retries:
                                    if not refreshed_after_401:
                                        logger.info("Attempting to refresh token via Telegram...")
                                        success = await self._update_token()
                                        if success:
                                            refreshed_after_401 = True
                                            logger.info("Token refreshed, retrying request...")
                                            continue
                                    if not switched_auth_format:
                                        switched = await self._toggle_authorization_format()
                                        if switched:
                                            switched_auth_format = True
                                            logger.info("Authorization format switched, retrying request...")
                                            continue
                            resp.raise_for_status()

                        return await resp.json()
                        
                except ClientResponseError as e:
                    if attempt == max_retries:
                        raise
                    logger.warning(
                        f"HTTP error {e.status} on {url} (attempt {attempt}/{max_retries})"
                    )
                    await asyncio.sleep(1 * attempt)
                except asyncio.TimeoutError:
                    if attempt == max_retries:
                        raise
                    logger.warning(
                        f"Timeout on {url} (attempt {attempt}/{max_retries})"
                    )
                    # Also backoff on timeout
                    self._global_pause_until = time.monotonic() + (2.0 * attempt)
                    await asyncio.sleep(1 * attempt)
                except Exception as e:
                    if attempt == max_retries:
                        raise
                    logger.warning(
                        f"Request error: {e} on {url} (attempt {attempt}/{max_retries})"
                    )
                    await asyncio.sleep(1 * attempt)

        raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts.")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # в”Ђв”Ђв”Ђ Collections в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    async def get_collections(self) -> list[dict[str, Any]]:
        """
        Fetch all gift collections with floor prices.
        GET /api/v1/gifts/collections
        """
        data = await self._request("GET", "/api/v1/gifts/collections")
        return data if isinstance(data, list) else []

    # в”Ђв”Ђв”Ђ Listings (Saling) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    async def get_listings(
        self,
        collection_names: list[str] | None = None,
        model_names: list[str] | None = None,
        low_to_high: bool = True,
        count: int = LISTINGS_PER_SCAN,
        cursor: str = "",
        min_price: int | None = None,
        max_price: int | None = None,
        ordering: str | None = "Price",
    ) -> dict[str, Any]:
        """
        Fetch active listings (gifts for sale).
        POST /api/v1/gifts/saling

        Args:
            collection_names: filter by collection names
            model_names: filter by model names
            low_to_high: sort by price ascending (True = cheapest first)
            count: number of listings to fetch
            cursor: pagination cursor from previous response
            min_price: minimum price in nanoTON
            max_price: maximum price in nanoTON
            ordering: sort type ("Price" or None for newest)

        Returns dict with keys:
            gifts: list of listing dicts
            cursor: pagination cursor for next page
            total: total count
        """
        # Request as many items as possible; fallback if backend rejects large pages.
        requested_count = max(1, min(100, int(count or LISTINGS_PER_SCAN)))
        count_candidates: list[int] = []
        for candidate in (requested_count, 50, 20):
            if candidate not in count_candidates:
                count_candidates.append(candidate)
        safe_cursor = str(cursor or "")
        last_exc: Exception | None = None

        for idx, candidate_count in enumerate(count_candidates, start=1):
            payload = {
                "count": candidate_count,
                "cursor": safe_cursor,
                "collectionNames": collection_names or [],
                "modelNames": model_names or [],
                "backdropNames": [],
                "symbolNames": [],
                "craftable": None,
                "giftType": None,
                "isCrafted": None,
                "isNew": None,
                "isPremarket": None,
                "isTransferable": None,
                "lowToHigh": low_to_high,
                "luckyBuy": None,
                "maxPrice": max_price,
                "minPrice": min_price,
                "number": None,
                "ordering": ordering,
                "query": None,
                "removeSelfSales": None,
                "tgCanBeCraftedFrom": None,
            }
            try:
                data = await self._request(
                    "POST",
                    "/api/v1/gifts/saling",
                    max_retries=2,
                    json=payload,
                )
                return (
                    data
                    if isinstance(data, dict)
                    else {"gifts": [], "cursor": "", "total": 0}
                )
            except ClientResponseError as exc:
                last_exc = exc
                if exc.status == 400 and idx < len(count_candidates):
                    logger.warning(
                        "Listings count=%s rejected with 400, trying fallback count...",
                        candidate_count,
                    )
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                if idx < len(count_candidates):
                    logger.warning(
                        "Listings request failed for count=%s, trying fallback: %s",
                        candidate_count,
                        exc,
                    )
                    continue
                raise

        if last_exc is not None:
            raise last_exc
        return {"gifts": [], "cursor": "", "total": 0}

    # в”Ђв”Ђв”Ђ Buy Gift в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    async def buy_gift(self, gift_id: str) -> dict[str, Any] | list | None:
        """
        Buy a specific gift by ID using the internal balance.
        POST /api/v1/gifts/buy  with payload {"Ids": ["<gift_id>"]}
        
        Returns the server response (list or dict) or None on error.
        An empty list [] typically means the gift was already sold.
        """
        url_path = "/api/v1/gifts/buy"
        payload_candidates: list[dict[str, Any]] = [
            {"Ids": [gift_id]},
            {"ids": [gift_id]},
            {"giftIds": [gift_id]},
        ]

        for idx, payload in enumerate(payload_candidates, start=1):
            try:
                logger.info(
                    "BUY REQUEST: POST %s | gift_id=%s | payload_variant=%s keys=%s",
                    url_path,
                    gift_id,
                    idx,
                    list(payload.keys()),
                )
                response = await self._request(
                    "POST",
                    url_path,
                    max_retries=2,
                    json=payload,
                )
                logger.info("BUY RESPONSE: %s", response)
                return response
            except ClientResponseError as e:
                if e.status == 400 and idx < len(payload_candidates):
                    logger.warning(
                        "Buy payload variant #%s rejected with 400 for %s, trying fallback...",
                        idx,
                        gift_id,
                    )
                    continue
                logger.error(f"Buy HTTP error for {gift_id}: {e.status} {e.message}")
                return None
            except Exception as e:
                if idx < len(payload_candidates):
                    logger.warning(
                        "Buy request failed for %s on payload variant #%s: %s; trying fallback...",
                        gift_id,
                        idx,
                        e,
                    )
                    continue
                logger.error(f"Buy request failed for {gift_id}: {e}")
                return None

        return None

    # в”Ђв”Ђв”Ђ My Gifts / Sell в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    async def get_my_gifts(
        self,
        *,
        is_listed: bool | None = None,
        cursor: str = "",
        count: int = 20,
    ) -> dict[str, Any]:
        """
        Fetch own gifts inventory.
        POST /api/v1/gifts
        """
        requested_count = int(count or 20)
        # Empirically MRKT list endpoints are limited to 20 items.
        safe_count = max(1, min(20, requested_count))
        listed_value = None if is_listed is None else bool(is_listed)
        cursor_value = str(cursor or "")

        # MRKT inventory endpoint is strict and may require both
        # PascalCase and camelCase field variants for filters.
        payload_candidates: list[dict[str, Any]] = [
            {
                "Count": safe_count,
                "Cursor": cursor_value,
                "IsListed": listed_value,
                "IsMine": True,
                "count": safe_count,
                "cursor": cursor_value,
                "isListed": listed_value,
                "isMine": True,
                "ModelNames": [],
                "SymbolNames": [],
                "BackdropNames": [],
                "CollectionNames": [],
                "modelNames": [],
                "symbolNames": [],
                "backdropNames": [],
                "collectionNames": [],
                "Craftable": None,
                "GiftType": None,
                "IsCrafted": None,
                "IsNew": None,
                "IsPremarket": None,
                "IsTransferable": None,
                "LowToHigh": False,
                "LuckyBuy": None,
                "MaxPrice": None,
                "MinPrice": None,
                "Number": None,
                "Ordering": "None",
                "Query": None,
                "RemoveSelfSales": None,
                "TgCanBeCraftedFrom": None,
                "craftable": None,
                "giftType": None,
                "isCrafted": None,
                "isNew": None,
                "isPremarket": None,
                "isTransferable": None,
                "lowToHigh": False,
                "luckyBuy": None,
                "maxPrice": None,
                "minPrice": None,
                "number": None,
                "ordering": "None",
                "lowToHigh": False,
                "query": None,
                "removeSelfSales": None,
                "tgCanBeCraftedFrom": None,
            },
            {
                "Count": safe_count,
                "Cursor": cursor_value,
                "IsListed": listed_value,
                "IsMine": True,
                "ModelNames": [],
                "SymbolNames": [],
                "BackdropNames": [],
                "CollectionNames": [],
            },
        ]

        last_exc: Exception | None = None
        for idx, payload in enumerate(payload_candidates, start=1):
            try:
                data = await self._request(
                    "POST",
                    "/api/v1/gifts",
                    max_retries=2,
                    json=payload,
                )
                return (
                    data
                    if isinstance(data, dict)
                    else {"gifts": [], "cursor": "", "total": 0}
                )
            except ClientResponseError as exc:
                last_exc = exc
                if exc.status == 400:
                    logger.warning(
                        "Inventory payload candidate #%s rejected with 400, trying fallback...",
                        idx,
                    )
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Inventory payload candidate #%s failed: %s",
                    idx,
                    exc,
                )
                continue

        logger.error(
            "Failed to fetch inventory from /api/v1/gifts after %s payload attempts: %s",
            len(payload_candidates),
            last_exc,
        )
        return {"gifts": [], "cursor": "", "total": 0, "_failed": True}

    async def sell_gifts(self, gift_ids: list[str], price_ton: float) -> dict[str, Any] | None:
        """
        List gifts for sale.
        POST /api/v1/gifts/sale
        """
        normalized_price_ton = normalize_market_price_ton(price_ton)
        price_nano = ton_to_nano(normalized_price_ton)
        if price_nano <= 0:
            logger.error(
                "Sell request skipped for %s: invalid non-positive price_ton=%s",
                gift_ids,
                normalized_price_ton,
            )
            return None

        payload = {
            "ids": gift_ids,
            "price": price_nano,
        }
        try:
            logger.info(
                "Sell request: ids=%s req_price_ton=%.9f normalized_price_ton=%.2f price_nano=%s",
                gift_ids,
                float(price_ton),
                normalized_price_ton,
                price_nano,
            )
            return await self._request("POST", "/api/v1/gifts/sale", json=payload)
        except Exception as exc:
            logger.error(
                "Sell request failed for %s (req_price_ton=%.9f, normalized_price_ton=%.2f, price_nano=%s): %s",
                gift_ids,
                float(price_ton),
                normalized_price_ton,
                price_nano,
                exc,
            )
            return None

    async def change_sale_price(
        self,
        gift_ids: list[str],
        new_price_ton: float,
    ) -> dict[str, Any] | None:
        """
        Change sale price.
        POST /api/v1/gifts/sale/change-price
        """
        normalized_price_ton = normalize_market_price_ton(new_price_ton)
        payload = {
            "ids": gift_ids,
            "newPrice": ton_to_nano(normalized_price_ton),
        }
        try:
            logger.info(
                "Change-price request: ids=%s req_new_price_ton=%.9f normalized_new_price_ton=%.2f new_price_nano=%s",
                gift_ids,
                float(new_price_ton),
                normalized_price_ton,
                payload["newPrice"],
            )
            return await self._request(
                "POST",
                "/api/v1/gifts/sale/change-price",
                json=payload,
            )
        except Exception as exc:
            logger.error(
                "Change-price request failed for %s (req_new_price_ton=%.9f, normalized_new_price_ton=%.2f, new_price_nano=%s): %s",
                gift_ids,
                float(new_price_ton),
                normalized_price_ton,
                payload["newPrice"],
                exc,
            )
            return None

    async def cancel_sale(self, gift_ids: list[str]) -> dict[str, Any] | None:
        """
        Cancel active sale.
        POST /api/v1/gifts/sale/cancel
        """
        payload = {"ids": gift_ids}
        try:
            return await self._request("POST", "/api/v1/gifts/sale/cancel", json=payload)
        except Exception as exc:
            logger.error("Cancel-sale request failed for %s: %s", gift_ids, exc)
            return None

    async def buy_more_sell_changes(self, gift_ids: list[str]) -> dict[str, Any] | None:
        """
        Buy additional 5 price-change attempts by spice.
        POST /api/v1/gifts/sell-by-spice
        """
        payload = {
            "giftsIds": gift_ids,
            "isSpicesChange": True,
        }
        try:
            return await self._request("POST", "/api/v1/gifts/sell-by-spice", json=payload)
        except Exception as exc:
            logger.error("Sell-by-spice request failed for %s: %s", gift_ids, exc)
            return None

    # в”Ђв”Ђв”Ђ Orders в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

    async def get_top_order(
        self,
        *,
        collection_name: str,
        model_name: str | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        """
        Fetch top order (best bid) for collection/model.
        POST /api/v1/orders/top
        """
        payload = {
            "collectionName": collection_name,
            "modelName": model_name,
        }
        try:
            return await self._request("POST", "/api/v1/orders/top", json=payload)
        except Exception as exc:
            logger.error(
                "Top-order request failed for collection=%s model=%s: %s",
                collection_name,
                model_name,
                exc,
            )
            return None

    async def get_orders(
        self,
        *,
        collection_name: str,
        cursor: str = "",
        count: int = 50,
    ) -> dict[str, Any] | None:
        """
        Fetch orders market depth.
        POST /api/v1/orders
        """
        payload = {
            "collectionName": collection_name,
            "cursor": cursor,
            "count": count,
        }
        try:
            data = await self._request("POST", "/api/v1/orders", json=payload)
            return data if isinstance(data, dict) else {"orders": [], "cursor": "", "total": 0}
        except Exception as exc:
            logger.error("Orders request failed for %s: %s", collection_name, exc)
            return None

    async def get_all_collection_top_orders(self) -> dict[str, Any] | list[Any] | None:
        """
        Fetch all collection top bids.
        GET /api/v1/orders/all-collection-top
        """
        try:
            return await self._request("GET", "/api/v1/orders/all-collection-top")
        except Exception as exc:
            logger.error("All-collection-top request failed: %s", exc)
            return None

    async def fill_order(
        self,
        *,
        order_id: str,
        gift_ids: list[str],
    ) -> dict[str, Any] | None:
        """
        Fill order instantly.
        POST /api/v1/orders/fill/
        """
        payload = {
            "orderId": order_id,
            "giftIds": gift_ids,
        }
        try:
            return await self._request("POST", "/api/v1/orders/fill/", json=payload)
        except Exception as exc:
            logger.error("Order fill failed order_id=%s gifts=%s: %s", order_id, gift_ids, exc)
            return None

    async def get_my_orders(self) -> dict[str, Any] | list[Any] | None:
        """
        Fetch own orders.
        POST /api/v1/orders/get-my-orders
        """
        try:
            return await self._request("POST", "/api/v1/orders/get-my-orders", json={})
        except Exception as exc:
            logger.error("Get-my-orders request failed: %s", exc)
            return None

