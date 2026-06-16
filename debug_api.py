"""Debug script — inspect actual saling API response."""
import asyncio
import json
from api import MRKTClient

async def debug():
    client = MRKTClient()
    try:
        # Fetch listings for Chill Flame (cheapest first)
        data = await client.get_listings(
            collection_names=["Chill Flame"],
            low_to_high=True,
            count=3,
        )
        print("=== RAW SALING RESPONSE ===")
        print(f"Top-level keys: {list(data.keys())}")
        print(f"Total: {data.get('total')}")
        print(f"Cursor: {data.get('cursor')}")
        
        gifts = data.get("gifts", [])
        print(f"\nGifts count: {len(gifts)}")
        
        if gifts:
            print("\n=== FIRST GIFT (ALL FIELDS) ===")
            print(json.dumps(gifts[0], indent=2, default=str))
            
            print("\n=== ALL GIFT KEYS ===")
            print(list(gifts[0].keys()))
        else:
            print("\nNO GIFTS RETURNED!")
            print("\n=== FULL RESPONSE ===")
            print(json.dumps(data, indent=2, default=str))

        # Also try collections
        cols = await client.get_collections()
        for c in cols:
            if c.get("title") in ("Chill Flame", "Vice Cream"):
                print(f"\n=== Collection: {c['title']} ===")
                print(f"  name: {c['name']}")
                print(f"  floor: {c.get('floorPriceNanoTons')}")
    finally:
        await client.close()

asyncio.run(debug())
