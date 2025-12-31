"""
Late-Game Snipe Configuration.

Thresholds for the final-seconds snipe strategy on 15-min crypto binaries.
"""

# Trading parameters
TRADE_SIZE = 20.0              # $ amount per snipe trade
STARTING_CAPITAL = 1000.0      # Initial paper trading capital

# Timing window (seconds before market close)
MIN_TIME_REMAINING = 10        # Don't enter if less than 10s left
MAX_TIME_REMAINING = 60        # Only activate in final 60s

# Price thresholds for entry
MIN_LEADER_PRICE = 0.70        # Leader must be at least 70%
MAX_LEADER_PRICE = 0.92        # Leader must be under 92% (room to profit)
MIN_EXPECTED_ROI = 0.08        # Minimum 8% expected ROI

# Spread filter (illiquidity protection)
MAX_SPREAD = 0.05              # Skip if spread > 5% (abnormal)

# Assets to trade (all available 15-min markets)
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Timing
TICK_INTERVAL = 0.5            # Seconds between price checks
MARKET_REFRESH_INTERVAL = 30.0 # Seconds between market discovery

# Persistence
DB_PATH = "sniper/data/trades.db"

# Dashboard
DASHBOARD_PORT = 5051
DASHBOARD_HOST = "0.0.0.0"
