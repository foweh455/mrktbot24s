"""
MRKT NFT Gift Sniper Bot — Configuration
"""

import os

# Load variables from a local .env file if python-dotenv is installed (optional).
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM AUTOMATION (AUTO-AUTH)
# ═══════════════════════════════════════════════════════════════

# Get these from https://my.telegram.org/auth
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))  # int, e.g. 12345678 — get from https://my.telegram.org/auth
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")  # string, e.g. "a1b2c3d4e5f6..."


# ═══════════════════════════════════════════════════════════════
#  API Settings
# ═══════════════════════════════════════════════════════════════

API_BASE_URL = "https://api.tgmrkt.io"
API_TOKEN = os.getenv("MRKT_API_TOKEN", "")

# Request headers matching the browser session
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Authorization": API_TOKEN,
    "Content-Type": "application/json",
    "Origin": "https://cdn.tgmrkt.io",
    "Referer": "https://cdn.tgmrkt.io/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}

COOKIES = {
    "access_token": API_TOKEN,
}

# ═══════════════════════════════════════════════════════════════
#  Scanner Settings
# ═══════════════════════════════════════════════════════════════

# Minimum discount (%) below floor price to flag as a deal
# e.g. 5 means listings at least 5% below floor
MIN_DISCOUNT_PERCENT = 2.0

# How many listings to fetch per collection per scan
LISTINGS_PER_SCAN = 50

# Scan interval in seconds (how often to poll the API)
SCAN_INTERVAL_SECONDS = 1

# If True, main sniper always starts in running mode after process restart,
# ignoring previously persisted pause state in runtime_state.json.
MAIN_SNIPER_FORCE_RUNNING_ON_START = True

# While paused, print pause status to console no more often than this interval.
PAUSE_STATUS_LOG_INTERVAL_SECONDS = 30

# Maximum concurrent requests to the API (increased for faster scanning)
MAX_CONCURRENT_REQUESTS = 7

# ═══════════════════════════════════════════════════════════════
#  Reliability improvements
# ═══════════════════════════════════════════════════════════════

# Floor refresh cadence (R1)
FLOOR_REFRESH_SECONDS = 12.0      # default cadence for Scanner.refresh_floor_prices
FLOOR_REFRESH_MIN_SECONDS = 5.0   # hard lower bound, even if user sets less

# Zero-rarity gate for regular floor sniper (R2)
AUTO_BUY_ZERO_RARITY_MIN_DISCOUNT_PERCENT = 30.0

# Auto-Sell price change budget (R3)
AUTO_SELL_PRICE_CHANGE_LIMIT_BASE = 4   # MRKT grants 4 free changes, not 5

# ═══════════════════════════════════════════════════════════════
#  Filtering
# ═══════════════════════════════════════════════════════════════

# If non-empty, ONLY scan these collections (by name/title)
# Leave empty to scan ALL collections
WHITELIST_COLLECTIONS: list[str] = []

# ═══════════════════════════════════════════════════════════════
#  Auto-Buy Settings
# ═══════════════════════════════════════════════════════════════
AUTO_BUY_ENABLED = True
AUTO_BUY_MAX_PRICE_TON = 6.0  # Failsafe: Never buy anything strictly above this TON amount (our balance)
AUTO_BUY_SKIP_TRADE_BAN = True
# Trade-ban filter:
# - < 3 days: allowed
# - 3..10 days: requires at least AUTO_BUY_TRADE_BAN_MIN_DISCOUNT_PERCENT discount
# - >= 10 days: always skip
AUTO_BUY_TRADE_BAN_DISCOUNT_REQUIRED_FROM_DAYS = 3.0
AUTO_BUY_TRADE_BAN_HARD_SKIP_DAYS = 10.0
AUTO_BUY_TRADE_BAN_MIN_DISCOUNT_PERCENT = 13.0

# Discount requirements based on price
AUTO_BUY_DISCOUNT_THRESHOLD_PRICE_TON = 4.0  # Price threshold for discount rules
AUTO_BUY_MIN_DISCOUNT_CHEAP = 4.0  # Discount required for gifts < 4 TON
AUTO_BUY_MIN_DISCOUNT_EXPENSIVE = 3.0  # Discount required for gifts >= 4 TON
AUTO_BUY_MIN_DISCOUNT_10_TO_20 = 3.0  # Discount required for gifts in [10, 20) TON
AUTO_BUY_MIN_DISCOUNT_20_PLUS = 3.0  # Discount required for gifts >= 20 TON

# Anti-dump protection for regular sniper mode (toggle in Telegram menu).
# Idea: avoid buys when market is crowded at the same (or lower) price,
# because floor often drops right after.
AUTO_BUY_DEPTH_GUARD_DEFAULT_ENABLED = False
AUTO_BUY_DEPTH_GUARD_MAX_AT_OR_BELOW = 3
AUTO_BUY_DEPTH_GUARD_NEAR_PRICE_PERCENT = 0.35
AUTO_BUY_DEPTH_GUARD_SCAN_COUNT = 30

# Runtime sniper stats thresholds
STATS_MISSED_BALANCE_MIN_PRICE_TON = 8.0
STATS_MISSED_BALANCE_MIN_DISCOUNT_PERCENT = 3.0

# Separate Rare-Model strategy (independent from regular discount logic)
# Must be enabled manually from Telegram menu each time bot starts.
RARE_MODEL_DEFAULT_ENABLED = False
RARE_MODEL_DEFAULT_BALANCE_TON = 6.0
RARE_MODEL_MAX_RARITY_PERCENT = 1.0
RARE_MODEL_MIN_LISTING_PRICE_TON = 5.0       # hard minimum: do not buy below 5 TON
RARE_MODEL_PRICE_THRESHOLD_TON = 10.0        # split premiums: [5,10) and >=10
RARE_MODEL_MAX_PREMIUM_5_TO_10_PERCENT = 10.0
RARE_MODEL_MAX_PREMIUM_10_PLUS_PERCENT = 10.0
RARE_MODEL_BELOW_FLOOR_FORCE_BUY = True      # for rare model below floor: buy regardless of regular discount thresholds

# Separate Auto-Sell strategy (independent from buy strategies)
AUTO_SELL_DEFAULT_ENABLED = False
AUTO_SELL_DEFAULT_BALANCE_TON = 1000.0
AUTO_SELL_PRICE_THRESHOLD_TON = 10.0
AUTO_SELL_TARGET_PROFIT_5_10_PERCENT = 2.0
AUTO_SELL_TARGET_PROFIT_10_PLUS_PERCENT = 2.0
AUTO_SELL_FLOOR_PREMIUM_5_10_PERCENT = 1.5
AUTO_SELL_FLOOR_PREMIUM_10_PLUS_PERCENT = 1.0
AUTO_SELL_RELIST_INTERVAL_SECONDS = 600
AUTO_SELL_STUCK_HOURS = 2.0
AUTO_SELL_PROMPT_PRICE_THRESHOLD_TON = 4.0
AUTO_SELL_LOSS_CAP_PERCENT = 4.0
AUTO_SELL_RARE_PROTECT_PERCENT = 1.0
AUTO_SELL_ORDER_MODE_CRITICAL_ONLY = True
AUTO_SELL_CRITICAL_PROMPT_TIMEOUT_SECONDS = 900
AUTO_SELL_BUY_MORE_CHANGES_COST_TON = 0.1

# Телеграм бот для уведомлений о покупке
TELEGRAM_NOTIFY_BOT_TOKEN = os.getenv("TELEGRAM_NOTIFY_BOT_TOKEN", "")
TELEGRAM_NOTIFY_CHAT_ID = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "")

# Collections to SKIP (by name/title)
BLACKLIST_COLLECTIONS: list[str] = ["Witch Hat"]

# Minimum floor price in TON to consider a collection worth scanning
# (skip very cheap collections where the spread is negligible)
MIN_FLOOR_PRICE_TON = 0.5

# ═══════════════════════════════════════════════════════════════
#  Display
# ═══════════════════════════════════════════════════════════════

# Enable sound alert on deal found (Windows only)
SOUND_ALERT = True

# Show all scanned collections in console (verbose mode)
VERBOSE = False
