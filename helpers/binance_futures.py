#!/usr/bin/env python3
"""
Binance Futures data for high-alpha features.

Provides: funding rate, open interest, liquidations, mark price.
"""
import asyncio
import json
import requests
import websockets
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from collections import deque

BINANCE_FUTURES_API = "https://fapi.binance.com"
BINANCE_FUTURES_WSS = "wss://fstream.binance.com"

# Asset to futures symbol mapping
FUTURES_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


@dataclass
class FuturesState:
    """Futures market state for an asset."""
    asset: str

    # Funding & Premium
    funding_rate: float = 0.0  # Current funding rate
    mark_price: float = 0.0
    index_price: float = 0.0

    # Open Interest
    open_interest: float = 0.0  # Current OI in contracts
    open_interest_value: float = 0.0  # OI in USDT
    oi_history: List[float] = field(default_factory=list)  # For OI change calc

    # Trade flow (CVD proxy)
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    cvd: float = 0.0  # Cumulative volume delta
    trade_count: int = 0

    # Trade intensity tracking (for 15-min features)
    trade_timestamps: List[float] = field(default_factory=list)  # Recent trade times
    large_trade_threshold: float = 0.0  # Dynamic threshold based on recent trades
    large_trade_flag: float = 0.0  # 1.0 if large trade just hit, decays
    recent_trade_sizes: List[float] = field(default_factory=list)  # For threshold calc

    # Liquidations
    recent_long_liqs: float = 0.0  # Long liquidation volume (last hour)
    recent_short_liqs: float = 0.0

    # Multi-timeframe
    returns_1m: float = 0.0
    returns_5m: float = 0.0
    returns_10m: float = 0.0  # Critical for 15-min expiries
    returns_15m: float = 0.0
    returns_1h: float = 0.0  # Extended timeframe

    # Volatility
    realized_vol_1h: float = 0.0  # Rolling 1h volatility

    # Volume
    volume_24h: float = 0.0
    volume_1h: float = 0.0

    last_update: Optional[datetime] = None

    @property
    def basis(self) -> float:
        """Futures premium/discount (mark - index) / index."""
        if self.index_price > 0:
            return (self.mark_price - self.index_price) / self.index_price
        return 0.0

    @property
    def oi_change_1h(self) -> float:
        """OI change over last hour (approximate from history)."""
        if len(self.oi_history) < 2:
            return 0.0
        # Compare current to oldest in buffer (assumes ~1hr of history)
        return (self.open_interest - self.oi_history[0]) / max(1, self.oi_history[0])

    @property
    def trade_flow_imbalance(self) -> float:
        """Buy vs sell volume imbalance [-1, 1]."""
        total = self.buy_volume + self.sell_volume
        if total == 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total

    @property
    def vol_ratio(self) -> float:
        """Recent volume vs 24h average."""
        avg_hourly = self.volume_24h / 24 if self.volume_24h > 0 else 1
        return self.volume_1h / max(1, avg_hourly)

    @property
    def liquidation_pressure(self) -> float:
        """Net liquidation pressure (positive = more longs liquidated)."""
        total = self.recent_long_liqs + self.recent_short_liqs
        if total == 0:
            return 0.0
        return (self.recent_long_liqs - self.recent_short_liqs) / total

    @property
    def trade_intensity(self) -> float:
        """Trades per second over last 10 seconds."""
        import time
        now = time.time()
        # Count trades in last 10 seconds
        recent = [t for t in self.trade_timestamps if now - t < 10]
        return len(recent) / 10.0


def fetch_funding_rate(asset: str) -> Optional[Dict]:
    """Fetch current funding rate and mark price."""
    symbol = FUTURES_SYMBOLS.get(asset)
    if not symbol:
        return None

    try:
        url = f"{BINANCE_FUTURES_API}/fapi/v1/premiumIndex?symbol={symbol}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "funding_rate": float(data["lastFundingRate"]),
                "mark_price": float(data["markPrice"]),
                "index_price": float(data["indexPrice"]),
            }
    except Exception as e:
        print(f"Error fetching funding rate for {asset}: {e}")
    return None


def fetch_open_interest(asset: str) -> Optional[Dict]:
    """Fetch current open interest."""
    symbol = FUTURES_SYMBOLS.get(asset)
    if not symbol:
        return None

    try:
        url = f"{BINANCE_FUTURES_API}/fapi/v1/openInterest?symbol={symbol}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "open_interest": float(data["openInterest"]),
            }
    except Exception as e:
        print(f"Error fetching OI for {asset}: {e}")
    return None


def fetch_klines(asset: str, interval: str = "1m", limit: int = 60) -> Optional[List]:
    """Fetch recent klines for price/volume data."""
    symbol = FUTURES_SYMBOLS.get(asset)
    if not symbol:
        return None

    try:
        url = f"{BINANCE_FUTURES_API}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"Error fetching klines for {asset}: {e}")
    return None


def compute_multi_tf_returns(klines_1m: List) -> Dict[str, float]:
    """Compute multi-timeframe returns from 1m klines."""
    if not klines_1m or len(klines_1m) < 15:
        return {"1m": 0.0, "5m": 0.0, "10m": 0.0, "15m": 0.0, "1h": 0.0, "realized_vol_1h": 0.0}

    current_close = float(klines_1m[-1][4])

    # 1m return
    close_1m_ago = float(klines_1m[-2][4]) if len(klines_1m) >= 2 else current_close
    ret_1m = (current_close - close_1m_ago) / close_1m_ago if close_1m_ago > 0 else 0.0

    # 5m return
    close_5m_ago = float(klines_1m[-6][4]) if len(klines_1m) >= 6 else current_close
    ret_5m = (current_close - close_5m_ago) / close_5m_ago if close_5m_ago > 0 else 0.0

    # 10m return (critical for 15-min expiries)
    close_10m_ago = float(klines_1m[-11][4]) if len(klines_1m) >= 11 else current_close
    ret_10m = (current_close - close_10m_ago) / close_10m_ago if close_10m_ago > 0 else 0.0

    # 15m return
    close_15m_ago = float(klines_1m[-16][4]) if len(klines_1m) >= 16 else current_close
    ret_15m = (current_close - close_15m_ago) / close_15m_ago if close_15m_ago > 0 else 0.0

    # 1h return
    close_1h_ago = float(klines_1m[-61][4]) if len(klines_1m) >= 61 else current_close
    ret_1h = (current_close - close_1h_ago) / close_1h_ago if close_1h_ago > 0 else 0.0

    # Realized volatility (std of 1m returns over last hour)
    realized_vol_1h = 0.0
    if len(klines_1m) >= 60:
        closes = [float(k[4]) for k in klines_1m[-60:]]
        returns = [(closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] > 0 else 0.0
                   for i in range(1, len(closes))]
        if returns:
            import numpy as np
            realized_vol_1h = float(np.std(returns) * np.sqrt(60))  # Annualize to hourly

    return {"1m": ret_1m, "5m": ret_5m, "10m": ret_10m, "15m": ret_15m, "1h": ret_1h, "realized_vol_1h": realized_vol_1h}


def compute_volume_stats(klines_1m: List) -> Dict[str, float]:
    """Compute volume statistics from klines."""
    if not klines_1m:
        return {"volume_1h": 0.0, "volume_24h": 0.0}

    # Sum volume from last 60 candles (1 hour)
    volume_1h = sum(float(k[5]) for k in klines_1m[-60:])

    # Estimate 24h from 1h (will be approximate)
    volume_24h = volume_1h * 24  # Rough estimate

    return {"volume_1h": volume_1h, "volume_24h": volume_24h}


class FuturesStreamer:
    """Stream futures data from Binance."""

    def __init__(self, assets: List[str] = None):
        self.assets = assets or ["BTC", "ETH", "SOL", "XRP"]
        self.states: Dict[str, FuturesState] = {}
        self.running = False

        # Trade flow tracking (rolling window)
        self._trade_windows: Dict[str, deque] = {}
        self._liq_windows: Dict[str, deque] = {}

        for asset in self.assets:
            self.states[asset] = FuturesState(asset=asset)
            self._trade_windows[asset] = deque(maxlen=1000)  # Last 1000 trades
            self._liq_windows[asset] = deque(maxlen=100)  # Last 100 liquidations

    def get_state(self, asset: str) -> Optional[FuturesState]:
        """Get futures state for an asset."""
        return self.states.get(asset)

    async def _poll_rest_data(self):
        """Periodically fetch REST data (funding, OI, klines)."""
        while self.running:
            for asset in self.assets:
                state = self.states.get(asset)
                if not state:
                    continue

                # Funding rate
                funding = fetch_funding_rate(asset)
                if funding:
                    state.funding_rate = funding["funding_rate"]
                    state.mark_price = funding["mark_price"]
                    state.index_price = funding["index_price"]

                # Open interest
                oi = fetch_open_interest(asset)
                if oi:
                    state.open_interest = oi["open_interest"]
                    state.oi_history.append(oi["open_interest"])
                    if len(state.oi_history) > 60:  # Keep ~1hr of history
                        state.oi_history = state.oi_history[-60:]

                # Klines for multi-TF returns and volume (fetch 65 for 1h returns)
                klines = fetch_klines(asset, "1m", 65)
                if klines:
                    returns = compute_multi_tf_returns(klines)
                    state.returns_1m = returns["1m"]
                    state.returns_5m = returns["5m"]
                    state.returns_10m = returns["10m"]
                    state.returns_15m = returns["15m"]
                    state.returns_1h = returns["1h"]
                    state.realized_vol_1h = returns["realized_vol_1h"]

                    vol_stats = compute_volume_stats(klines)
                    state.volume_1h = vol_stats["volume_1h"]
                    state.volume_24h = vol_stats["volume_24h"]

                state.last_update = datetime.now(timezone.utc)

            # Poll every 10 seconds
            await asyncio.sleep(10)

    async def _stream_trades(self):
        """Stream aggregate trades for CVD calculation."""
        symbols = [FUTURES_SYMBOLS[a].lower() for a in self.assets if a in FUTURES_SYMBOLS]
        streams = "/".join([f"{s}@aggTrade" for s in symbols])
        url = f"{BINANCE_FUTURES_WSS}/stream?streams={streams}"

        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            data = json.loads(msg)

                            if "data" in data:
                                import time
                                trade = data["data"]
                                symbol = trade["s"]
                                price = float(trade["p"])
                                qty = float(trade["q"])
                                trade_value = qty * price
                                is_buyer_maker = trade["m"]  # True = sell, False = buy

                                # Find asset
                                for asset, sym in FUTURES_SYMBOLS.items():
                                    if sym == symbol:
                                        state = self.states.get(asset)
                                        if state:
                                            if is_buyer_maker:
                                                state.sell_volume += trade_value
                                            else:
                                                state.buy_volume += trade_value
                                            state.cvd = state.buy_volume - state.sell_volume
                                            state.trade_count += 1

                                            # Track trade timestamps for intensity
                                            now = time.time()
                                            state.trade_timestamps.append(now)
                                            # Keep only last 30 seconds of timestamps
                                            state.trade_timestamps = [t for t in state.trade_timestamps if now - t < 30]

                                            # Track trade sizes for large trade detection
                                            state.recent_trade_sizes.append(trade_value)
                                            if len(state.recent_trade_sizes) > 100:
                                                state.recent_trade_sizes = state.recent_trade_sizes[-100:]

                                            # Update large trade threshold (2x median of recent trades)
                                            if len(state.recent_trade_sizes) >= 20:
                                                import numpy as np
                                                median_size = np.median(state.recent_trade_sizes)
                                                state.large_trade_threshold = median_size * 3  # 3x median = large

                                            # Detect large trade
                                            if state.large_trade_threshold > 0 and trade_value > state.large_trade_threshold:
                                                state.large_trade_flag = 1.0
                                        break

                        except asyncio.TimeoutError:
                            pass

            except Exception as e:
                print(f"Futures trade stream error: {e}")
                await asyncio.sleep(1)

    async def _stream_liquidations(self):
        """Stream liquidation orders."""
        symbols = [FUTURES_SYMBOLS[a].lower() for a in self.assets if a in FUTURES_SYMBOLS]
        streams = "/".join([f"{s}@forceOrder" for s in symbols])
        url = f"{BINANCE_FUTURES_WSS}/stream?streams={streams}"

        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            data = json.loads(msg)

                            if "data" in data:
                                order = data["data"]["o"]
                                symbol = order["s"]
                                side = order["S"]  # SELL = long liq, BUY = short liq
                                qty = float(order["q"])
                                price = float(order["p"])
                                value = qty * price

                                # Find asset
                                for asset, sym in FUTURES_SYMBOLS.items():
                                    if sym == symbol:
                                        state = self.states.get(asset)
                                        if state:
                                            if side == "SELL":
                                                state.recent_long_liqs += value
                                            else:
                                                state.recent_short_liqs += value

                                            # Record in window for decay
                                            self._liq_windows[asset].append({
                                                "time": datetime.now(timezone.utc),
                                                "side": side,
                                                "value": value
                                            })
                                        break

                        except asyncio.TimeoutError:
                            pass

            except Exception as e:
                # Liquidations might not always be available
                await asyncio.sleep(5)

    async def _decay_volumes(self):
        """Periodically decay volume/liq counters (rolling window effect)."""
        while self.running:
            await asyncio.sleep(5)  # Every 5 seconds for faster decay of large_trade_flag

            for asset in self.assets:
                state = self.states.get(asset)
                if state:
                    # Fast decay for large trade flag (half-life ~10 seconds)
                    state.large_trade_flag *= 0.7

            await asyncio.sleep(55)  # Complete the minute

            for asset in self.assets:
                state = self.states.get(asset)
                if state:
                    # Decay by 10% per minute (effectively ~1hr half-life)
                    state.buy_volume *= 0.9
                    state.sell_volume *= 0.9
                    state.recent_long_liqs *= 0.9
                    state.recent_short_liqs *= 0.9
                    state.cvd = state.buy_volume - state.sell_volume

    async def stream(self):
        """Start all futures data streams."""
        self.running = True

        print("Starting Binance Futures streams...")

        # Initial data fetch
        for asset in self.assets:
            state = self.states.get(asset)
            if state:
                funding = fetch_funding_rate(asset)
                if funding:
                    state.funding_rate = funding["funding_rate"]
                    state.mark_price = funding["mark_price"]
                    print(f"  {asset}: funding={state.funding_rate:.4%}, mark=${state.mark_price:,.2f}")

        # Run all streams concurrently
        await asyncio.gather(
            self._poll_rest_data(),
            self._stream_trades(),
            self._stream_liquidations(),
            self._decay_volumes(),
        )

    def stop(self):
        """Stop all streams."""
        self.running = False


# Quick fetch functions for one-shot data
def get_futures_snapshot(asset: str) -> Optional[FuturesState]:
    """Get a snapshot of futures data for an asset (non-streaming)."""
    state = FuturesState(asset=asset)

    # Funding
    funding = fetch_funding_rate(asset)
    if funding:
        state.funding_rate = funding["funding_rate"]
        state.mark_price = funding["mark_price"]
        state.index_price = funding["index_price"]

    # OI
    oi = fetch_open_interest(asset)
    if oi:
        state.open_interest = oi["open_interest"]

    # Klines
    klines = fetch_klines(asset, "1m", 65)
    if klines:
        returns = compute_multi_tf_returns(klines)
        state.returns_1m = returns["1m"]
        state.returns_5m = returns["5m"]
        state.returns_10m = returns["10m"]
        state.returns_15m = returns["15m"]
        state.returns_1h = returns["1h"]
        state.realized_vol_1h = returns["realized_vol_1h"]

        vol_stats = compute_volume_stats(klines)
        state.volume_1h = vol_stats["volume_1h"]
        state.volume_24h = vol_stats["volume_24h"]

    state.last_update = datetime.now(timezone.utc)
    return state


if __name__ == "__main__":
    # Test
    print("Fetching futures data...")

    for asset in ["BTC", "ETH", "SOL"]:
        state = get_futures_snapshot(asset)
        if state:
            print(f"\n{asset}:")
            print(f"  Funding: {state.funding_rate:.4%}")
            print(f"  Mark: ${state.mark_price:,.2f}")
            print(f"  OI: {state.open_interest:,.0f}")
            print(f"  Returns: 1m={state.returns_1m:.3%} 5m={state.returns_5m:.3%} 15m={state.returns_15m:.3%}")
            print(f"  Vol 1h: ${state.volume_1h:,.0f}")
