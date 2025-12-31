"""
Late-Game Snipe Configuration.

Thresholds for the final-seconds snipe strategy on 15-min crypto binaries.
All parameters can be modified at runtime via the dashboard.
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List

CONFIG_FILE = "sniper/data/config.json"


@dataclass
class SnipeSettings:
    """Mutable snipe parameters - can be changed at runtime."""

    # Timing window (seconds before market close)
    min_time_remaining: int = 10          # Don't enter if less than this
    max_time_remaining: int = 180         # Only activate in final N seconds

    # Price thresholds for entry
    min_leader_price: float = 0.75        # Leader must be at least this
    max_leader_price: float = 0.95        # Leader must be under this
    min_expected_roi: float = 0.05        # Minimum expected ROI

    # Trade sizing
    trade_size: float = 20.0              # $ amount per snipe trade

    # Spread filter (illiquidity protection)
    max_spread: float = 0.05              # Skip if spread > this

    # Observation mode - log without trading
    observation_mode: bool = True         # When True, only log opportunities

    # Assets to trade
    assets: List[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP"])

    def save(self):
        """Save settings to config file."""
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> 'SnipeSettings':
        """Load settings from config file, or return defaults."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                return cls(**data)
            except Exception as e:
                print(f"Error loading config: {e}, using defaults")
        return cls()

    def update(self, **kwargs):
        """Update settings and save."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.save()


# Global mutable settings instance
settings = SnipeSettings.load()

# Static config (not changeable at runtime)
STARTING_CAPITAL = 1000.0
TICK_INTERVAL = 0.5
MARKET_REFRESH_INTERVAL = 30.0
DB_PATH = "sniper/data/trades.db"
OBSERVATION_CSV = "sniper/data/observations.csv"
DASHBOARD_PORT = 5051
DASHBOARD_HOST = "0.0.0.0"

# Legacy aliases for backwards compatibility
TRADE_SIZE = settings.trade_size
MIN_TIME_REMAINING = settings.min_time_remaining
MAX_TIME_REMAINING = settings.max_time_remaining
MIN_LEADER_PRICE = settings.min_leader_price
MAX_LEADER_PRICE = settings.max_leader_price
MIN_EXPECTED_ROI = settings.min_expected_roi
MAX_SPREAD = settings.max_spread
ASSETS = settings.assets
