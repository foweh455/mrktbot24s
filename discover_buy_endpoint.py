"""Discover buy API endpoint - writes results to JSON."""
import asyncio
import aiohttp
import json
import sys
import os

os.environ["PYTHONIOENCODING"] = "utf-8"

from config import API_BASE_URL, HEADERS, COOKIES
from api import MRKTClient

async def main():
    results = []
    
    client = MRKTClient()
    try:
        data = await client.get_listings(low_to_high=True, count=1)
        gifts = data.get("gifts", [])
        if not gifts:
            results.append({"error": "No listings found"})
            with open("discover_results.json", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=True)
            return

        gift = gifts[0]
        gift_id = gift["id"]
        price = gift.get("salePrice", 0)
        title = gift.get("collectionTitle", gift.get("title", "?"))
        results.append({"target": title, "id": gift_id, "price": int(price)})
    finally:
        await client.close()

    endpoints = [
        ("POST", "/api/v1/gifts/buy", {"giftId": gift_id}),
        ("POST", "/api/v1/gifts/buy", {"id": gift_id}),
        ("POST", f"/api/v1/gifts/{gift_id}/buy", {}),
        ("POST", f"/api/v1/gifts/{gift_id}/buy", {"giftId": gift_id}),
        ("POST", "/api/v1/gifts/purchase", {"giftId": gift_id}),
        ("POST", f"/api/v1/gifts/{gift_id}/purchase", {}),
        ("POST", "/api/v1/market/buy", {"giftId": gift_id}),
        ("POST", "/api/v1/market/purchase", {"giftId": gift_id}),
        ("POST", f"/api/v1/market/buy/{gift_id}", {}),
        ("POST", "/api/v1/order/create", {"giftId": gift_id}),
        ("POST", "/api/v1/orders/buy", {"giftId": gift_id}),
        ("POST", "/api/v1/gifts/saling/buy", {"giftId": gift_id}),
        ("POST", f"/api/v1/gifts/saling/{gift_id}/buy", {}),
        ("POST", "/api/v1/gifts/buy", {"giftId": gift_id, "price": int(price)}),
        ("POST", "/api/v1/gifts/buy", {"id": gift_id, "price": int(price)}),
        ("PUT", f"/api/v1/gifts/{gift_id}/buy", {}),
        ("PUT", "/api/v1/gifts/buy", {"giftId": gift_id}),
    ]

    async with aiohttp.ClientSession(headers=HEADERS, cookies=COOKIES) as session:
        for method, path, payload in endpoints:
            url = f"{API_BASE_URL}{path}"
            try:
                async with session.request(method, url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    body = await resp.text()
                    status = resp.status
                    results.append({
                        "method": method,
                        "path": path,
                        "payload": payload,
                        "status": status,
                        "response": body[:500]
                    })
            except Exception as e:
                results.append({
                    "method": method,
                    "path": path,
                    "payload": payload,
                    "error": str(e)
                })

    with open("discover_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=True)
    
    print("Done! Results in discover_results.json")

if __name__ == "__main__":
    asyncio.run(main())
