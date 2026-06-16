"""
MRKT NFT Gift Sniper Bot — Console Display & Alerts
Rich console output for listing notifications.
"""

import sys
import logging
from datetime import datetime

from colorama import Fore, Style, init as colorama_init

from scanner import Listing, nano_to_ton

logger = logging.getLogger("mrkt.bot")

# Initialize colorama for Windows support
colorama_init(autoreset=True)


BANNER = f"""
{Fore.CYAN}{Style.BRIGHT}
  ╔═══════════════════════════════════════════════════════════════════════╗
  ║                                                                       ║
  ║   ███╗   ███╗██████╗ ██╗  ██╗████████╗   ██████╗  ██████╗ ████████╗   ║
  ║   ████╗ ████║██╔══██╗██║ ██╔╝╚══██╔══╝   ██╔══██╗██╔═══██╗╚══██╔══╝   ║
  ║   ██╔████╔██║██████╔╝█████╔╝    ██║      ██████╔╝██║   ██║   ██║      ║
  ║   ██║╚██╔╝██║██╔══██╗██╔═██╗    ██║      ██╔══██╗██║   ██║   ██║      ║
  ║   ██║ ╚═╝ ██║██║  ██║██║  ██╗   ██║      ██████╔╝╚██████╔╝   ██║      ║
  ║   ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝      ╚═════╝  ╚═════╝    ╚═╝      ║
  ║                                                                       ║
  ║                 🎯  NFT Gift Sniper Bot  🎯                           ║
  ║                                                                       ║
  ╚═══════════════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}"""


def print_banner() -> None:
    print(BANNER)


def print_separator() -> None:
    print(f"{Fore.BLUE}{'─' * 70}{Style.RESET_ALL}")


def print_status(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"  {Fore.WHITE}[{now}]{Style.RESET_ALL} {message}")


def print_scan_start(num_collections: int) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(
        f"\n  {Fore.BLUE}[{now}] 🔍 Мониторинг глобальной ленты новинок (отслеживаем {num_collections} колл.)...{Style.RESET_ALL}"
    )


def print_scan_complete(num_new: int, num_deals: int, elapsed: float) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    if num_deals > 0:
        color = Fore.RED + Style.BRIGHT
        icon = "🔥"
        extra = f" | {num_deals} НИЖЕ ФЛОРА!"
    elif num_new > 0:
        color = Fore.GREEN
        icon = "📦"
        extra = ""
    else:
        color = Fore.YELLOW
        icon = "✅"
        extra = ""
    print(
        f"  {color}[{now}] {icon} Скан за {elapsed:.1f}s — "
        f"{num_new} новых листингов{extra}{Style.RESET_ALL}"
    )


def print_floor_prices(floor_prices: dict[str, int], titles: dict[str, str]) -> None:
    print(f"\n  {Fore.CYAN}{Style.BRIGHT}📊 Отслеживаемые коллекции:{Style.RESET_ALL}")
    print_separator()

    sorted_items = sorted(floor_prices.items(), key=lambda x: x[1], reverse=True)

    for name, price_nano in sorted_items:
        title = titles.get(name, name)
        price_ton = nano_to_ton(price_nano)
        print(
            f"    {Fore.WHITE}{title:<25}{Style.RESET_ALL} "
            f"Floor: {Fore.GREEN}{price_ton:.2f} TON{Style.RESET_ALL}"
        )

    print_separator()


# ═══════════════════════════════════════════════════════════════
#  Listing Display
# ═══════════════════════════════════════════════════════════════

def print_listing(listing: Listing) -> None:
    """Print a single listing — highlighted if below floor."""
    now = datetime.now().strftime("%H:%M:%S")

    if listing.is_below_floor:
        # BELOW FLOOR — big alert
        dc = Fore.RED + Style.BRIGHT
        print()
        print(f"  {dc}╔{'═' * 66}╗{Style.RESET_ALL}")
        print(
            f"  {dc}║  🔥🔥🔥 НИЖЕ ФЛОРА на {listing.discount_percent:.1f}%! 🔥🔥🔥"
            f"{' ' * max(0, 42 - len(f'НИЖЕ ФЛОРА на {listing.discount_percent:.1f}%!'))}║{Style.RESET_ALL}"
        )
        print(f"  {dc}╠{'═' * 66}╣{Style.RESET_ALL}")
        print(f"  {dc}║{Style.RESET_ALL}  📦 Коллекция:  {Fore.WHITE}{Style.BRIGHT}{listing.collection_title}{Style.RESET_ALL}")
        print(f"  {dc}║{Style.RESET_ALL}  💰 Цена:       {Fore.GREEN}{Style.BRIGHT}{listing.listing_price_ton:.4f} TON{Style.RESET_ALL}")
        print(f"  {dc}║{Style.RESET_ALL}  📈 Флор:       {Fore.YELLOW}{listing.floor_price_ton:.4f} TON{Style.RESET_ALL}")
        print(f"  {dc}║{Style.RESET_ALL}  📉 Скидка:     {dc}{listing.discount_percent:.2f}%{Style.RESET_ALL}")
        if listing.model_name:
            print(f"  {dc}║{Style.RESET_ALL}  🎨 Модель:     {Fore.CYAN}{listing.model_name}{Style.RESET_ALL}")
        if listing.number is not None:
            print(f"  {dc}║{Style.RESET_ALL}  🔢 Номер:      {Fore.CYAN}#{listing.number}{Style.RESET_ALL}")
        print(f"  {dc}║{Style.RESET_ALL}  🆔 ID:         {Fore.BLUE}{listing.gift_id}{Style.RESET_ALL}")
        print(f"  {dc}╚{'═' * 66}╝{Style.RESET_ALL}")
    else:
        # Above floor — compact line
        markup = ((listing.listing_price_nano - listing.floor_price_nano) / listing.floor_price_nano) * 100
        model_str = f" [{listing.model_name}]" if listing.model_name else ""
        num_str = f" #{listing.number}" if listing.number is not None else ""
        print(
            f"  {Fore.WHITE}[{now}]{Style.RESET_ALL} "
            f"📦 {Fore.CYAN}{listing.collection_title}{Style.RESET_ALL}{model_str}{num_str} "
            f"— {Fore.YELLOW}{listing.listing_price_ton:.2f} TON{Style.RESET_ALL} "
            f"(флор {listing.floor_price_ton:.2f}, "
            f"{Fore.RED}+{markup:.1f}%{Style.RESET_ALL})"
        )


def print_listings(below_floor: list[Listing], all_new: list[Listing]) -> None:
    """Print all new listings, with below-floor highlighted first."""
    # Print below-floor deals first (big alerts)
    for listing in below_floor:
        print_listing(listing)


# ═══════════════════════════════════════════════════════════════
#  Sound & Utility
# ═══════════════════════════════════════════════════════════════

def play_alert_sound() -> None:
    try:
        if sys.platform == "win32":
            import winsound
            winsound.Beep(1000, 200)
            winsound.Beep(1200, 200)
            winsound.Beep(1500, 300)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


def print_error(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"  {Fore.RED}[{now}] ❌ {message}{Style.RESET_ALL}")


def print_waiting(seconds: float) -> None:
    print(
        f"  {Fore.BLUE}⏳ Следующий скан через {seconds:.0f}s...{Style.RESET_ALL}",
        end="\r",
    )
