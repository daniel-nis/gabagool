"""
Gabagool Strategy - Asymmetric Scalping for Polymarket 15-min Crypto Binaries.

Goal: Accumulate positions where avg_cost_yes + avg_cost_no < 1.00
      This creates "locked profit" - guaranteed profit regardless of outcome.

How it works:
1. Track moving average of YES and NO prices independently
2. When YES price dips below MA * (1 - threshold), buy YES shares
3. When NO price dips below MA * (1 - threshold), buy NO shares
4. Each matched pair (1 YES + 1 NO) guarantees $1 payout
5. If total cost < $1, profit is locked

CRITICAL RULES (v2 - fixes for profitability):
- Trade cooldown: minimum 30s between same-side trades
- Balance priority: prefer buying under-represented side
- Expiry awareness: stop trading when <3 min left
- Longer MA: use 60+ periods for real dip detection

Example:
- Buy 10 YES shares at avg $0.45 = $4.50 total cost
- Buy 10 NO shares at avg $0.48 = $4.80 total cost
- Total cost: $9.30 for 10 matched pairs
- Guaranteed payout: $10.00 (either YES or NO wins)
- Locked profit: $0.70 (7.5% return)
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque
from datetime import datetime, timezone, timedelta
from enum import Enum


class GabagoolAction(Enum):
    """Actions the strategy can take."""
    HOLD = "hold"
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"


@dataclass
class GabagoolPosition:
    """Position tracking for a single market."""
    market_id: str
    asset: str

    # YES side
    shares_yes: float = 0.0
    cost_yes: float = 0.0  # Total $ spent on YES

    # NO side
    shares_no: float = 0.0
    cost_no: float = 0.0  # Total $ spent on NO

    # Trade history for this market
    trades: List[dict] = field(default_factory=list)

    # Cooldown tracking (v2)
    last_yes_trade: datetime = None
    last_no_trade: datetime = None

    @property
    def avg_price_yes(self) -> float:
        """Average price paid per YES share."""
        if self.shares_yes == 0:
            return 0.0
        return self.cost_yes / self.shares_yes

    @property
    def avg_price_no(self) -> float:
        """Average price paid per NO share."""
        if self.shares_no == 0:
            return 0.0
        return self.cost_no / self.shares_no

    @property
    def matched_shares(self) -> float:
        """Number of matched YES+NO pairs."""
        return min(self.shares_yes, self.shares_no)

    @property
    def locked_profit(self) -> float:
        """
        Guaranteed profit from matched pairs.
        Each matched pair pays $1. Profit = $1 * matched - cost_of_matched.
        """
        matched = self.matched_shares
        if matched == 0:
            return 0.0

        # Cost of matched shares
        # Use proportional cost based on shares used
        cost_matched_yes = (matched / self.shares_yes) * self.cost_yes if self.shares_yes > 0 else 0
        cost_matched_no = (matched / self.shares_no) * self.cost_no if self.shares_no > 0 else 0

        total_cost = cost_matched_yes + cost_matched_no
        payout = matched * 1.00

        return payout - total_cost

    @property
    def unmatched_yes(self) -> float:
        """Unmatched YES shares (at risk)."""
        return max(0, self.shares_yes - self.shares_no)

    @property
    def unmatched_no(self) -> float:
        """Unmatched NO shares (at risk)."""
        return max(0, self.shares_no - self.shares_yes)

    @property
    def total_exposure(self) -> float:
        """Total $ at risk (unmatched positions)."""
        # Unmatched shares have potential to lose their full cost
        if self.shares_yes > self.shares_no:
            unmatched_cost = (self.unmatched_yes / self.shares_yes) * self.cost_yes if self.shares_yes > 0 else 0
        else:
            unmatched_cost = (self.unmatched_no / self.shares_no) * self.cost_no if self.shares_no > 0 else 0
        return unmatched_cost

    def add_yes(self, shares: float, price: float, trade_size: float):
        """Add YES shares to position."""
        self.shares_yes += shares
        self.cost_yes += trade_size
        self.last_yes_trade = datetime.now(timezone.utc)  # v2: track cooldown
        self.trades.append({
            'time': datetime.now(timezone.utc).isoformat(),
            'side': 'YES',
            'shares': shares,
            'price': price,
            'cost': trade_size,
        })

    def add_no(self, shares: float, price: float, trade_size: float):
        """Add NO shares to position."""
        self.shares_no += shares
        self.cost_no += trade_size
        self.last_no_trade = datetime.now(timezone.utc)  # v2: track cooldown
        self.trades.append({
            'time': datetime.now(timezone.utc).isoformat(),
            'side': 'NO',
            'shares': shares,
            'price': price,
            'cost': trade_size,
        })


@dataclass
class PriceHistory:
    """Price history for a market."""
    yes_prices: deque = field(default_factory=lambda: deque(maxlen=100))
    no_prices: deque = field(default_factory=lambda: deque(maxlen=100))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=100))

    def add(self, yes_price: float, no_price: float):
        """Add price observation."""
        self.yes_prices.append(yes_price)
        self.no_prices.append(no_price)
        self.timestamps.append(datetime.now(timezone.utc))

    def ma_yes(self, periods: int = 20) -> Optional[float]:
        """Moving average of YES prices."""
        if len(self.yes_prices) < periods:
            return None
        recent = list(self.yes_prices)[-periods:]
        return sum(recent) / len(recent)

    def ma_no(self, periods: int = 20) -> Optional[float]:
        """Moving average of NO prices."""
        if len(self.no_prices) < periods:
            return None
        recent = list(self.no_prices)[-periods:]
        return sum(recent) / len(recent)


class GabagoolStrategy:
    """
    Asymmetric scalping - buy dips on BOTH sides independently.
    Goal: accumulate positions where avg_cost_yes + avg_cost_no < 1.00

    Parameters:
        dip_threshold: Buy when price < MA * (1 - threshold). Default 5%.
        lookback_periods: Periods for moving average. Default 60 (was 20).
        min_periods: Minimum observations before trading. Default 30 (was 10).
        max_position_per_side: Max $ to invest per side per market. Default $50 (was $100).
        trade_cooldown_seconds: Minimum seconds between same-side trades. Default 30.
        min_time_left_minutes: Stop trading when less than this many minutes left. Default 3.
        imbalance_threshold: If one side has > this ratio more cost, only buy other side. Default 1.5.
    """

    def __init__(
        self,
        dip_threshold: float = 0.05,
        lookback_periods: int = 60,  # v2: increased from 20
        min_periods: int = 30,  # v2: increased from 10
        max_position_per_side: float = 50.0,  # v2: reduced from 100
        trade_cooldown_seconds: float = 30.0,  # v2: new
        min_time_left_minutes: float = 3.0,  # v2: new
        imbalance_threshold: float = 1.5,  # v2: new
    ):
        self.dip_threshold = dip_threshold
        self.lookback = lookback_periods
        self.min_periods = min_periods
        self.max_position_per_side = max_position_per_side
        self.trade_cooldown = timedelta(seconds=trade_cooldown_seconds)  # v2
        self.min_time_left = timedelta(minutes=min_time_left_minutes)  # v2
        self.imbalance_threshold = imbalance_threshold  # v2

        # Per-market tracking
        self.positions: Dict[str, GabagoolPosition] = {}
        self.price_history: Dict[str, PriceHistory] = {}

        # Stats
        self.total_yes_buys = 0
        self.total_no_buys = 0

    def get_or_create_position(self, market_id: str, asset: str) -> GabagoolPosition:
        """Get or create position for a market."""
        if market_id not in self.positions:
            self.positions[market_id] = GabagoolPosition(
                market_id=market_id,
                asset=asset,
            )
        return self.positions[market_id]

    def get_or_create_history(self, market_id: str) -> PriceHistory:
        """Get or create price history for a market."""
        if market_id not in self.price_history:
            self.price_history[market_id] = PriceHistory()
        return self.price_history[market_id]

    def on_price_update(
        self,
        market_id: str,
        asset: str,
        yes_price: float,
        no_price: float,
        trade_size: float = 10.0,
        time_left: timedelta = None,  # v2: expiry awareness
    ) -> Tuple[GabagoolAction, Optional[dict]]:
        """
        Process price update and decide action.

        Args:
            market_id: Unique market identifier
            asset: Asset symbol (BTC, ETH, etc.)
            yes_price: Current YES token price (0-1)
            no_price: Current NO token price (0-1)
            trade_size: $ amount per trade
            time_left: Time remaining until market expiry (v2)

        Returns:
            Tuple of (action, trade_details or None)
        """
        now = datetime.now(timezone.utc)

        # Update price history
        history = self.get_or_create_history(market_id)
        history.add(yes_price, no_price)

        # Need minimum observations
        if len(history.yes_prices) < self.min_periods:
            return GabagoolAction.HOLD, None

        # v2: Check expiry - stop trading near market end
        if time_left is not None and time_left < self.min_time_left:
            return GabagoolAction.HOLD, None

        # Get position
        position = self.get_or_create_position(market_id, asset)

        # Calculate MAs
        ma_yes = history.ma_yes(self.lookback)
        ma_no = history.ma_no(self.lookback)

        if ma_yes is None or ma_no is None:
            return GabagoolAction.HOLD, None

        # Check for dips
        yes_dip_target = ma_yes * (1 - self.dip_threshold)
        no_dip_target = ma_no * (1 - self.dip_threshold)

        yes_is_dip = yes_price < yes_dip_target
        no_is_dip = no_price < no_dip_target

        # v2: Check cooldowns
        yes_on_cooldown = (
            position.last_yes_trade is not None and
            (now - position.last_yes_trade) < self.trade_cooldown
        )
        no_on_cooldown = (
            position.last_no_trade is not None and
            (now - position.last_no_trade) < self.trade_cooldown
        )

        # v2: Check max position limits
        yes_maxed = position.cost_yes >= self.max_position_per_side
        no_maxed = position.cost_no >= self.max_position_per_side

        # v2: Can we buy each side?
        can_buy_yes = yes_is_dip and not yes_on_cooldown and not yes_maxed
        can_buy_no = no_is_dip and not no_on_cooldown and not no_maxed

        if not can_buy_yes and not can_buy_no:
            return GabagoolAction.HOLD, None

        # v2: Calculate imbalance and determine which side to prioritize
        # If one side is significantly larger, ONLY buy the smaller side
        yes_cost = position.cost_yes
        no_cost = position.cost_no

        # Determine which side needs more buying based on imbalance
        force_yes_only = False
        force_no_only = False

        if yes_cost > 0 and no_cost > 0:
            if yes_cost / no_cost > self.imbalance_threshold:
                # YES is too heavy, only buy NO
                force_no_only = True
            elif no_cost / yes_cost > self.imbalance_threshold:
                # NO is too heavy, only buy YES
                force_yes_only = True
        elif yes_cost > 0 and no_cost == 0:
            # Have YES but no NO - strongly prefer NO
            force_no_only = True
        elif no_cost > 0 and yes_cost == 0:
            # Have NO but no YES - strongly prefer YES
            force_yes_only = True

        # Apply imbalance rules
        if force_no_only and can_buy_no:
            can_buy_yes = False
        elif force_yes_only and can_buy_yes:
            can_buy_no = False

        # Decide action
        action = GabagoolAction.HOLD
        trade_info = None

        if can_buy_yes and can_buy_no:
            # Both sides available - buy the bigger dip
            yes_dip_pct = (ma_yes - yes_price) / ma_yes
            no_dip_pct = (ma_no - no_price) / ma_no

            if no_dip_pct > yes_dip_pct:
                # Buy NO
                shares = trade_size / no_price
                position.add_no(shares, no_price, trade_size)
                action = GabagoolAction.BUY_NO
                self.total_no_buys += 1
                trade_info = {
                    'side': 'NO',
                    'price': no_price,
                    'shares': shares,
                    'cost': trade_size,
                    'ma': ma_no,
                    'dip_pct': no_dip_pct,
                }
            else:
                # Buy YES
                shares = trade_size / yes_price
                position.add_yes(shares, yes_price, trade_size)
                action = GabagoolAction.BUY_YES
                self.total_yes_buys += 1
                trade_info = {
                    'side': 'YES',
                    'price': yes_price,
                    'shares': shares,
                    'cost': trade_size,
                    'ma': ma_yes,
                    'dip_pct': yes_dip_pct,
                }
        elif can_buy_yes:
            # Only YES available
            shares = trade_size / yes_price
            position.add_yes(shares, yes_price, trade_size)
            action = GabagoolAction.BUY_YES
            self.total_yes_buys += 1
            trade_info = {
                'side': 'YES',
                'price': yes_price,
                'shares': shares,
                'cost': trade_size,
                'ma': ma_yes,
                'dip_pct': (ma_yes - yes_price) / ma_yes,
            }
        elif can_buy_no:
            # Only NO available
            shares = trade_size / no_price
            position.add_no(shares, no_price, trade_size)
            action = GabagoolAction.BUY_NO
            self.total_no_buys += 1
            trade_info = {
                'side': 'NO',
                'price': no_price,
                'shares': shares,
                'cost': trade_size,
                'ma': ma_no,
                'dip_pct': (ma_no - no_price) / ma_no,
            }

        return action, trade_info

    def calculate_locked_profit(self, market_id: str) -> float:
        """Get locked profit for a market."""
        if market_id not in self.positions:
            return 0.0
        return self.positions[market_id].locked_profit

    def get_total_locked_profit(self) -> float:
        """Total locked profit across all markets."""
        return sum(pos.locked_profit for pos in self.positions.values())

    def get_total_exposure(self) -> float:
        """Total $ at risk from unmatched positions."""
        return sum(pos.total_exposure for pos in self.positions.values())

    def get_position_summary(self, market_id: str) -> Optional[dict]:
        """Get position summary for a market."""
        if market_id not in self.positions:
            return None

        pos = self.positions[market_id]
        return {
            'market_id': market_id,
            'asset': pos.asset,
            'shares_yes': pos.shares_yes,
            'shares_no': pos.shares_no,
            'avg_price_yes': pos.avg_price_yes,
            'avg_price_no': pos.avg_price_no,
            'cost_yes': pos.cost_yes,
            'cost_no': pos.cost_no,
            'matched_shares': pos.matched_shares,
            'locked_profit': pos.locked_profit,
            'unmatched_yes': pos.unmatched_yes,
            'unmatched_no': pos.unmatched_no,
            'total_exposure': pos.total_exposure,
        }

    def clear_expired_market(self, market_id: str) -> Optional[dict]:
        """
        Clear position for an expired market and return final P&L.
        Called when market resolves.
        """
        if market_id not in self.positions:
            return None

        pos = self.positions[market_id]
        summary = self.get_position_summary(market_id)

        # The locked profit is guaranteed
        # Unmatched shares result in loss of their cost
        summary['final_pnl'] = pos.locked_profit - pos.total_exposure

        # Clean up
        del self.positions[market_id]
        if market_id in self.price_history:
            del self.price_history[market_id]

        return summary

    def get_stats(self) -> dict:
        """Get overall strategy stats."""
        return {
            'total_yes_buys': self.total_yes_buys,
            'total_no_buys': self.total_no_buys,
            'active_markets': len(self.positions),
            'total_locked_profit': self.get_total_locked_profit(),
            'total_exposure': self.get_total_exposure(),
            'positions': {
                mid: self.get_position_summary(mid)
                for mid in self.positions.keys()
            },
        }


if __name__ == "__main__":
    # Simple test
    strategy = GabagoolStrategy(dip_threshold=0.05, lookback_periods=10, min_periods=5)

    # Simulate price updates
    market_id = "test_market"
    asset = "BTC"

    # Build up price history
    for i in range(10):
        yes_price = 0.50 + (i % 3) * 0.01  # Oscillate
        no_price = 0.50 - (i % 3) * 0.01
        action, info = strategy.on_price_update(market_id, asset, yes_price, no_price)
        print(f"Tick {i}: YES={yes_price:.3f} NO={no_price:.3f} -> {action.value}")

    # Simulate a dip
    print("\n--- Simulating YES dip ---")
    action, info = strategy.on_price_update(market_id, asset, 0.42, 0.52)
    print(f"Action: {action.value}")
    if info:
        print(f"Trade: {info}")

    print("\n--- Simulating NO dip ---")
    action, info = strategy.on_price_update(market_id, asset, 0.48, 0.44)
    print(f"Action: {action.value}")
    if info:
        print(f"Trade: {info}")

    print("\n--- Position Summary ---")
    print(strategy.get_position_summary(market_id))

    print("\n--- Strategy Stats ---")
    print(strategy.get_stats())
