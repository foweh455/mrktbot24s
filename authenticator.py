"""
MRKT Telegram Authenticator
Uses Pyrogram to log into Telegram, request the Web App view for @mrkt,
and exchange the init_data for an active API token.
"""

import sys
from urllib.parse import unquote
import logging
import aiohttp

from telethon import TelegramClient
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import InputBotAppShortName

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, HEADERS

logger = logging.getLogger("mrkt.auth")


async def fetch_new_mrkt_token() -> str | None:
    """
    Logs in to Telegram (or uses existing session),
    requests the MRKT web view, and gets a new API token.
    """
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error("TELEGRAM_API_ID or TELEGRAM_API_HASH not set in config.py")
        return None

    logger.info("Connecting to Telegram to fetch fresh MRKT token...")
    
    # Telethon client. Will use mrkt_telethon_session.session file.
    client = TelegramClient('mrkt_telethon_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)

    try:
        await client.start()
        
        # Get bot info
        bot = await client.get_input_entity('mrkt')
        
        # Request web view data
        web_view = await client(RequestAppWebViewRequest(
            peer=bot,
            app=InputBotAppShortName(bot_id=bot, short_name="app"),
            platform="android",
            write_allowed=True,
        ))

        # Extract init_data
        init_data = unquote(web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])
        
        await client.disconnect()

        # Exchange init_data for MRKT token
        logger.info("Got Telegram init_data. Exchanging for MRKT token...")
        auth_data = {"data": init_data}
        
        clean_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://cdn.tgmrkt.io/",
            "Origin": "https://cdn.tgmrkt.io",
            "User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")
        }

        async with aiohttp.ClientSession(headers=clean_headers) as session:
            async with session.post("https://api.tgmrkt.io/api/v1/auth", json=auth_data) as resp:
                resp.raise_for_status()
                data = await resp.json()
                token = data.get("token")
                
                if token:
                    logger.info("Successfully obtained new MRKT API token!")
                    return token
                else:
                    logger.error("Auth response didn't contain 'token'.")
                    return None

    except Exception as e:
        logger.error(f"Failed to fetch new MRKT token: {e}")
        return None


if __name__ == "__main__":
    # If run standalone, it will trigger the login prompt and generate mrkt_session.session
    import asyncio
    
    # Setup basic logging for standalone run
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("❌ ОШИБКА: Сначала заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в config.py!")
        sys.exit(1)
        
    print("🚀 Авторизация в Telegram...")
    print("Если вы запускаете это впервые, введите номер телефона и код из Telegram.")
    
    async def test():
        token = await fetch_new_mrkt_token()
        if token:
            print(f"\n✅ УРА! Ваш новый MRKT токен: {token[:15]}... (скрыто)")
            print("Теперь вы можете запускать бота (main.py)!")
        else:
            print("\n❌ Не удалось получить токен.")
            
    asyncio.run(test())
