"""Test the buy endpoint with the correct field name 'Ids'."""
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
            print("No listings found")
            return
        gift = gifts[0]
        gift_id = gift["id"]
        price = gift.get("salePrice", 0)
        title = gift.get("collectionTitle", gift.get("title", "?"))
        results.append({"target": title, "id": gift_id, "price": int(price)})
    finally:
        await client.close()

    # Try the correct field name 'Ids' in different formats
    test_payloads = [
        {"Ids": [gift_id]},                          # Array with one ID
        {"Ids": gift_id},                             # Single string
        {"ids": [gift_id]},                           # lowercase
        {"Ids": [gift_id], "price": int(price)},      # With price
        {"Ids": [gift_id], "Price": int(price)},      # With Price capitalized
    ]

    async with aiohttp.ClientSession(headers=HEADERS, cookies=COOKIES) as session:
        for payload in test_payloads:
            url = f"{API_BASE_URL}/api/v1/gifts/buy"
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    body = await resp.text()
                    status = resp.status
                    results.append({
                        "payload": payload,
                        "status": status,
                        "response": body[:500]
                    })
            except Exception as e:
                results.append({
                    "payload": payload,
                    "error": str(e)
                })

    with open("test_buy_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=True)
    
    print("Done! Results in test_buy_results.json")

if __name__ == "__main__":
    asyncio.run(main())
