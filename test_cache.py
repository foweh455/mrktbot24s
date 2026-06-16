import asyncio
from api import MRKTClient
import time

async def test_cache():
    client = MRKTClient()
    
    print("Requesting 1...")
    res1 = await client.get_listings(["Chill Flame"], count=10)
    gifts1 = res1.get("gifts", [])
    if gifts1: print(f"1st request top item: {gifts1[0].get('id')} - {gifts1[0].get('salePrice')}")
    
    print("Waiting 3s...")
    await asyncio.sleep(3)
    
    print("Requesting 2 with timestamp dummy param...")
    
    # Overriding the URL just for the test
    original_request = client._request
    async def busted_request(method, endpoint, **kwargs):
        if "?" in endpoint:
            endpoint += f"&t={int(time.time() * 1000)}"
        else:
            endpoint += f"?t={int(time.time() * 1000)}"
        return await original_request(method, endpoint, **kwargs)
        
    client._request = busted_request
    
    res2 = await client.get_listings(["Chill Flame"], count=10)
    gifts2 = res2.get("gifts", [])
    if gifts2: print(f"2nd request top item: {gifts2[0].get('id')} - {gifts2[0].get('salePrice')}")
    
    print(f"Are they exactly identical dicts? {res1 == res2}")
    
    await client.close()

asyncio.run(test_cache())
