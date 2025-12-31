"""
Gabagool Configuration.

Edit this file to customize the bot settings.
These are the default values - override via command line or environment.
"""

# Trading parameters
STARTING_CAPITAL = 1000.0      # Initial paper trading capital
TRADE_SIZE = 10.0              # $ amount per dip buy
DIP_THRESHOLD = 0.05           # 5% below MA triggers buy
LOOKBACK_PERIODS = 20          # MA calculation window
MIN_PERIODS = 10               # Min observations before trading
MAX_POSITION_PER_SIDE = 100.0  # Max $ invested per side per market

# Assets to trade (15-min binaries available for these)
ASSETS = ["BTC", "ETH"]
# Other available: "SOL", "XRP"

# Timing
TICK_INTERVAL = 0.5            # Seconds between price checks
MARKET_REFRESH_INTERVAL = 60.0 # Seconds between market discovery

# Persistence
DB_PATH = "data/gabagool.db"
CSV_PATH = "logs/trades.csv"

# Dashboard
DASHBOARD_PORT = 5050
DASHBOARD_HOST = "0.0.0.0"

# Live trading (future)
# Set these to switch from paper to live
LIVE_TRADING = False
API_KEY = ""
API_SECRET = ""
PASSPHRASE = ""
