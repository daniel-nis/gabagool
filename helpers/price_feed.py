"""
Alternative price feed for crypto spot prices.

Uses CoinGecko (free, no geo-restrictions) when Binance is unavailable.
"""
import requests
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict
import time

# CoinGecko API (free, no auth required)
COINGECKO_API = "https://api.coingecko.com/api/v3"

# Asset to CoinGecko ID mapping
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
}

# Cache for rate limiting
_price_cache: Dict[str, tuple] = {}  # asset -> (price, timestamp)
CACHE_TTL = 10  # seconds


@dataclass
class PriceState:
    """Simple price state for an asset."""
    asset: str
    price: float
    price_15m_ago: Optional[float] = None
    change_15m: Optional[float] = None
    last_update: Optional[datetime] = None

    @property
    def direction(self) -> Optional[str]:
        """UP or DOWN based on 15m change."""
        if self.change_15m is None:
            return None
        return "UP" if self.change_15m > 0 else "DOWN"


def get_price(asset: str) -> Optional[float]:
    """Get current spot price for an asset."""
    global _price_cache

    # Check cache
    if asset in _price_cache:
        cached_price, cached_time = _price_cache[asset]
        if time.time() - cached_time < CACHE_TTL:
            return cached_price

    coin_id = COINGECKO_IDS.get(asset.upper())
    if not coin_id:
        return None

    try:
        url = f"{COINGECKO_API}/simple/price?ids={coin_id}&vs_currencies=usd"
        resp = requests.get(url, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            price = data.get(coin_id, {}).get("usd")
            if price:
                _price_cache[asset] = (price, time.time())
                return price

    except Exception as e:
        print(f"CoinGecko price fetch error for {asset}: {e}")

    return None


def get_price_with_history(asset: str) -> Optional[PriceState]:
    """Get current price with 15-minute historical comparison."""
    coin_id = COINGECKO_IDS.get(asset.upper())
    if not coin_id:
        return None

    try:
        # Get current price
        current_price = get_price(asset)
        if not current_price:
            return None

        state = PriceState(
            asset=asset.upper(),
            price=current_price,
            last_update=datetime.now(timezone.utc)
        )

        # Try to get historical price (15 mins ago)
        # CoinGecko market_chart with 1h range gives ~1min granularity
        url = f"{COINGECKO_API}/coins/{coin_id}/market_chart?vs_currency=usd&days=0.02"
        resp = requests.get(url, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            prices = data.get("prices", [])

            if len(prices) >= 2:
                # Find price from ~15 mins ago
                now_ms = time.time() * 1000
                target_ms = now_ms - (15 * 60 * 1000)  # 15 mins ago

                # Find closest price point
                closest = min(prices, key=lambda p: abs(p[0] - target_ms))
                price_15m_ago = closest[1]

                state.price_15m_ago = price_15m_ago
                state.change_15m = (current_price - price_15m_ago) / price_15m_ago

        return state

    except Exception as e:
        print(f"CoinGecko history fetch error for {asset}: {e}")

        # Return at least current price
        current_price = get_price(asset)
        if current_price:
            return PriceState(
                asset=asset.upper(),
                price=current_price,
                last_update=datetime.now(timezone.utc)
            )

    return None


def test_connection() -> bool:
    """Test if CoinGecko API is accessible."""
    try:
        resp = requests.get(f"{COINGECKO_API}/ping", timeout=5)
        return resp.status_code == 200
    except:
        return False


if __name__ == "__main__":
    print("Testing CoinGecko price feed...")

    if not test_connection():
        print("CoinGecko API not accessible!")
        exit(1)

    print("API accessible!")

    for asset in ["BTC", "ETH", "SOL"]:
        state = get_price_with_history(asset)
        if state:
            print(f"\n{asset}:")
            print(f"  Current: ${state.price:,.2f}")
            if state.price_15m_ago:
                print(f"  15m ago: ${state.price_15m_ago:,.2f}")
                print(f"  Change: {state.change_15m:+.2%}")
                print(f"  Direction: {state.direction}")
        else:
            print(f"\n{asset}: Failed to fetch")
