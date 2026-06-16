import asyncio
import json
from api import MRKTClient

async def debug():
    client = MRKTClient()
    try:
        data1 = await client._get_session()
        
        # Test global feed without collection names, ordered by newest
        payload2 = {
            "count": 50, "cursor": "", "collectionNames": [],
            "modelNames": [], "backdropNames": [], "symbolNames": [],
            "lowToHigh": False, "ordering": None
        }
        async with data1.post("https://api.tgmrkt.io/api/v1/gifts/saling", json=payload2) as resp:
            try:
                d2 = await resp.json()
                gifts = d2.get("gifts", [])
                
                print(f"Global Feed (size {len(gifts)}):")
                for g in gifts[:10]:
                    print(f" - {g.get('collectionTitle')} | ID: {g.get('id')} | Price: {int(g.get('salePrice', 0)) // 10**9} TON")
            except Exception as e:
                print(f"Newest failed: {await resp.text()}")

    finally:
        await client.close()

asyncio.run(debug())
