"""
Gabagool Strategy v4 - Opportunistic Dip-Buying for Polymarket 15-min Crypto Binaries.

Core Insight: Buy YES and NO dips independently, then match pairs to lock profit
when combined cost < $1.00.

Example Flow:
1. BTC YES dips to $0.45 → buy 22 shares ($10)
2. 30s later, BTC NO dips to $0.48 → buy 20 shares ($10)
3. FIFO match: 20 shares paired at $0.45 + $0.48 = $0.93
4. Locked profit: 20 × $0.07 = $1.40 guaranteed
5. Remaining: 2 unmatched YES shares (at risk until matched or sold)

KEY RULES:
1. Buy dips on either side independently
2. Track unmatched shares separately from matched pairs
3. FIFO matching when both sides have unmatched shares
4. Close unmatched positions before expiry (sell back)
5. Locked profit only for matched pairs with combined < $1
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from enum import Enum


class GabagoolAction(Enum):
    """Actions the strategy can take."""
    HOLD = "hold"
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    SELL_YES = "sell_yes"  # Close unmatched near expiry
    SELL_NO = "sell_no"    # Close unmatched near expiry


@dataclass
class ShareLot:
    """A single purchase lot for cost basis tracking (FIFO)."""
    shares: float
    price: float
    timestamp: datetime

    @property
    def cost(self) -> float:
        return self.shares * self.price


@dataclass
class DipBuyPosition:
    """Tracks position with separate unmatched and matched tracking."""
    market_id: str
    asset: str

    # Unmatched shares - FIFO queues
    unmatched_yes_lots: List[ShareLot] = field(default_factory=list)
    unmatched_no_lots: List[ShareLot] = field(default_factory=list)

    # Matched pairs (locked profit)
    matched_shares: float = 0.0
    matched_cost_yes: float = 0.0
    matched_cost_no: float = 0.0
    locked_profit: float = 0.0

    # Price history for MA calculation
    yes_price_history: List[Tuple[datetime, float]] = field(default_factory=list)
    no_price_history: List[Tuple[datetime, float]] = field(default_factory=list)

    # Cooldowns (separate for each side)
    last_yes_trade_time: Optional[datetime] = None
    last_no_trade_time: Optional[datetime] = None

    # Trade history
    trades: List[dict] = field(default_factory=list)

    @property
    def unmatched_yes_shares(self) -> float:
        """Total unmatched YES shares."""
        return sum(lot.shares for lot in self.unmatched_yes_lots)

    @property
    def unmatched_no_shares(self) -> float:
        """Total unmatched NO shares."""
        return sum(lot.shares for lot in self.unmatched_no_lots)

    @property
    def unmatched_yes_cost(self) -> float:
        """Total cost of unmatched YES shares."""
        return sum(lot.cost for lot in self.unmatched_yes_lots)

    @property
    def unmatched_no_cost(self) -> float:
        """Total cost of unmatched NO shares."""
        return sum(lot.cost for lot in self.unmatched_no_lots)

    @property
    def total_cost(self) -> float:
        """Total invested capital."""
        return (self.unmatched_yes_cost + self.unmatched_no_cost +
                self.matched_cost_yes + self.matched_cost_no)

    @property
    def unrealized_risk(self) -> float:
        """Capital at risk (unmatched positions could lose everything)."""
        return self.unmatched_yes_cost + self.unmatched_no_cost

    @property
    def matched_combined_cost(self) -> float:
        """Combined cost per matched share."""
        if self.matched_shares == 0:
            return 0.0
        return (self.matched_cost_yes + self.matched_cost_no) / self.matched_shares

    def add_yes_lot(self, shares: float, price: float) -> None:
        """Add a new YES purchase lot."""
        now = datetime.now(timezone.utc)
        self.unmatched_yes_lots.append(ShareLot(shares, price, now))
        self.last_yes_trade_time = now
        self.trades.append({
            'time': now.isoformat(),
            'side': 'YES',
            'shares': shares,
            'price': price,
            'cost': shares * price,
            'type': 'buy',
        })

    def add_no_lot(self, shares: float, price: float) -> None:
        """Add a new NO purchase lot."""
        now = datetime.now(timezone.utc)
        self.unmatched_no_lots.append(ShareLot(shares, price, now))
        self.last_no_trade_time = now
        self.trades.append({
            'time': now.isoformat(),
            'side': 'NO',
            'shares': shares,
            'price': price,
            'cost': shares * price,
            'type': 'buy',
        })

    def match_shares(self) -> Tuple[float, float]:
        """
        Match unmatched YES and NO shares using FIFO.
        Returns (shares_matched, profit_locked).

        IMPORTANT: Only matches pairs where combined cost < $1.00 (profitable).
        Unprofitable pairs are left unmatched to avoid locking in losses.
        """
        total_matched = 0.0
        newly_locked_profit = 0.0

        while self.unmatched_yes_lots and self.unmatched_no_lots:
            yes_lot = self.unmatched_yes_lots[0]
            no_lot = self.unmatched_no_lots[0]

            # Calculate cost basis for this potential match
            yes_cost_per_share = yes_lot.price
            no_cost_per_share = no_lot.price
            combined_cost_per_share = yes_cost_per_share + no_cost_per_share

            # CRITICAL: Only match if profitable (combined < $1.00)
            if combined_cost_per_share >= 1.0:
                # Skip this pair - it would lock in a loss
                # Leave both lots unmatched, hoping prices improve
                break

            # Match the smaller of the two lots
            shares_to_match = min(yes_lot.shares, no_lot.shares)

            if shares_to_match < 0.001:  # Skip tiny amounts
                break

            # Calculate profit (guaranteed positive since combined < $1)
            profit_per_share = 1.0 - combined_cost_per_share
            profit = shares_to_match * profit_per_share

            # Update matched totals
            self.matched_shares += shares_to_match
            self.matched_cost_yes += shares_to_match * yes_cost_per_share
            self.matched_cost_no += shares_to_match * no_cost_per_share
            self.locked_profit += profit
            newly_locked_profit += profit
            total_matched += shares_to_match

            # Reduce lot sizes
            yes_lot.shares -= shares_to_match
            no_lot.shares -= shares_to_match

            # Remove depleted lots
            if yes_lot.shares < 0.001:
                self.unmatched_yes_lots.pop(0)
            if no_lot.shares < 0.001:
                self.unmatched_no_lots.pop(0)

            # Record match
            self.trades.append({
                'time': datetime.now(timezone.utc).isoformat(),
                'type': 'match',
                'shares': shares_to_match,
                'yes_price': yes_cost_per_share,
                'no_price': no_cost_per_share,
                'combined': combined_cost_per_share,
                'profit_locked': profit,
            })

        return total_matched, newly_locked_profit

    def remove_yes_shares(self, shares: float, price: float) -> float:
        """Remove YES shares (for selling). Returns actual shares removed."""
        removed = 0.0
        while shares > 0.001 and self.unmatched_yes_lots:
            lot = self.unmatched_yes_lots[0]
            take = min(lot.shares, shares)
            lot.shares -= take
            shares -= take
            removed += take
            if lot.shares < 0.001:
                self.unmatched_yes_lots.pop(0)

        self.trades.append({
            'time': datetime.now(timezone.utc).isoformat(),
            'side': 'YES',
            'shares': removed,
            'price': price,
            'type': 'sell',
        })
        return removed

    def remove_no_shares(self, shares: float, price: float) -> float:
        """Remove NO shares (for selling). Returns actual shares removed."""
        removed = 0.0
        while shares > 0.001 and self.unmatched_no_lots:
            lot = self.unmatched_no_lots[0]
            take = min(lot.shares, shares)
            lot.shares -= take
            shares -= take
            removed += take
            if lot.shares < 0.001:
                self.unmatched_no_lots.pop(0)

        self.trades.append({
            'time': datetime.now(timezone.utc).isoformat(),
            'side': 'NO',
            'shares': removed,
            'price': price,
            'type': 'sell',
        })
        return removed


class GabagoolStrategy:
    """
    Opportunistic Dip-Buying Strategy v4.

    Parameters:
        yes_buy_threshold: Buy YES when below this price (default 0.48)
        no_buy_threshold: Buy NO when below this price (default 0.48)
        use_moving_average: Also check MA for dip detection
        ma_window_seconds: MA calculation window (default 30s)
        dip_below_ma_pct: Buy when this % below MA (default 0.03 = 3%)
        trade_size: $ per individual buy (default $10)
        max_unmatched_cost: Max $ unmatched per side (default $50)
        max_position_cost: Total max $ per market (default $200)
        cooldown_seconds: Per-side cooldown (default 5s)
        close_before_expiry_mins: Close unmatched before expiry (default 2 min)
    """

    def __init__(
        self,
        yes_buy_threshold: float = 0.48,
        no_buy_threshold: float = 0.48,
        use_moving_average: bool = True,
        ma_window_seconds: float = 30.0,
        dip_below_ma_pct: float = 0.03,
        trade_size: float = 10.0,
        max_unmatched_cost: float = 50.0,
        max_position_cost: float = 200.0,
        cooldown_seconds: float = 5.0,
        close_before_expiry_mins: float = 2.0,
    ):
        self.yes_buy_threshold = yes_buy_threshold
        self.no_buy_threshold = no_buy_threshold
        self.use_moving_average = use_moving_average
        self.ma_window_seconds = ma_window_seconds
        self.dip_below_ma_pct = dip_below_ma_pct
        self.trade_size = trade_size
        self.max_unmatched_cost = max_unmatched_cost
        self.max_position_cost = max_position_cost
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.close_before_expiry = timedelta(minutes=close_before_expiry_mins)

        # Per-market tracking
        self.positions: Dict[str, DipBuyPosition] = {}

        # Stats
        self.total_trades = 0
        self.total_profit_locked = 0.0

    def get_or_create_position(self, market_id: str, asset: str) -> DipBuyPosition:
        """Get or create position for a market."""
        if market_id not in self.positions:
            self.positions[market_id] = DipBuyPosition(
                market_id=market_id,
                asset=asset,
            )
        return self.positions[market_id]

    def _update_price_history(
        self,
        position: DipBuyPosition,
        yes_price: float,
        no_price: float,
        now: datetime,
    ) -> None:
        """Update price history and trim old entries."""
        position.yes_price_history.append((now, yes_price))
        position.no_price_history.append((now, no_price))

        # Trim old entries (keep 2x MA window for safety)
        cutoff = now - timedelta(seconds=self.ma_window_seconds * 2)
        position.yes_price_history = [
            (ts, p) for ts, p in position.yes_price_history if ts >= cutoff
        ]
        position.no_price_history = [
            (ts, p) for ts, p in position.no_price_history if ts >= cutoff
        ]

    def _calculate_ma(
        self,
        history: List[Tuple[datetime, float]],
        now: datetime,
    ) -> Optional[float]:
        """Calculate moving average from recent price history."""
        cutoff = now - timedelta(seconds=self.ma_window_seconds)
        recent = [price for ts, price in history if ts >= cutoff]

        if len(recent) < 5:  # Need minimum data points
            return None

        return sum(recent) / len(recent)

    def _check_dip_signal(
        self,
        position: DipBuyPosition,
        side: str,
        current_price: float,
        now: datetime,
    ) -> bool:
        """Check if current price represents a dip worth buying.

        IMPORTANT: Price MUST be at or below the absolute threshold.
        MA dip detection is only used as an additional filter when below threshold.
        This prevents buying at high prices that would create unprofitable matches.
        """
        threshold = self.yes_buy_threshold if side == 'YES' else self.no_buy_threshold

        # Hard cap: NEVER buy above threshold (prevents >100% combined matches)
        if current_price > threshold:
            return False

        # Below threshold - either buy on simple threshold or MA dip
        if not self.use_moving_average:
            # Simple mode: buy whenever below threshold
            return True

        # MA mode: prefer to buy when it's also an MA dip (extra confirmation)
        history = (position.yes_price_history if side == 'YES'
                   else position.no_price_history)
        ma = self._calculate_ma(history, now)

        if ma is None:
            # Not enough data for MA - fall back to threshold
            return True

        # Buy if price is below MA (dip) or significantly below threshold
        dip_threshold = ma * (1 - self.dip_below_ma_pct)
        if current_price < dip_threshold:
            return True

        # Also buy if we're well below threshold even if not an MA dip
        if current_price < threshold * 0.95:
            return True

        return False

    def _can_buy_yes(self, position: DipBuyPosition, now: datetime) -> bool:
        """Check if we can buy YES (cooldown and limits)."""
        # Cooldown check
        if position.last_yes_trade_time is not None:
            if now - position.last_yes_trade_time < self.cooldown:
                return False

        # Unmatched limit
        if position.unmatched_yes_cost >= self.max_unmatched_cost:
            return False

        # Total position limit
        if position.total_cost >= self.max_position_cost:
            return False

        return True

    def _can_buy_no(self, position: DipBuyPosition, now: datetime) -> bool:
        """Check if we can buy NO (cooldown and limits)."""
        # Cooldown check
        if position.last_no_trade_time is not None:
            if now - position.last_no_trade_time < self.cooldown:
                return False

        # Unmatched limit
        if position.unmatched_no_cost >= self.max_unmatched_cost:
            return False

        # Total position limit
        if position.total_cost >= self.max_position_cost:
            return False

        return True

    def on_price_update(
        self,
        market_id: str,
        asset: str,
        yes_price: float,
        no_price: float,
        time_left: timedelta = None,
    ) -> Tuple[GabagoolAction, Optional[dict]]:
        """
        Main strategy method - check for dip opportunities.

        Returns:
            Tuple of (action, trade_details or None)
        """
        now = datetime.now(timezone.utc)
        position = self.get_or_create_position(market_id, asset)

        # Update price history
        self._update_price_history(position, yes_price, no_price, now)

        # Try to match any unmatched shares
        matched, profit = position.match_shares()
        if matched > 0:
            self.total_profit_locked += profit

        # Check if we need to close unmatched before expiry
        if time_left is not None and time_left < self.close_before_expiry:
            return self._check_close_unmatched(position, yes_price, no_price)

        # Skip new buys if market closing soon (< 3 min)
        if time_left is not None and time_left < timedelta(minutes=3):
            return GabagoolAction.HOLD, None

        # Check for dip opportunities
        yes_dip = self._check_dip_signal(position, 'YES', yes_price, now)
        no_dip = self._check_dip_signal(position, 'NO', no_price, now)

        # Prioritize the side with bigger dip (lower price = bigger opportunity)
        # Or prioritize the side that helps balance unmatched positions
        if yes_dip and no_dip:
            # Both sides dipping - buy the one we have less of (to balance)
            if position.unmatched_yes_shares <= position.unmatched_no_shares:
                if self._can_buy_yes(position, now):
                    return self._execute_buy_yes(position, yes_price)
            else:
                if self._can_buy_no(position, now):
                    return self._execute_buy_no(position, no_price)

        # Single side dip
        if yes_dip and self._can_buy_yes(position, now):
            return self._execute_buy_yes(position, yes_price)

        if no_dip and self._can_buy_no(position, now):
            return self._execute_buy_no(position, no_price)

        return GabagoolAction.HOLD, None

    def _execute_buy_yes(
        self,
        position: DipBuyPosition,
        price: float,
    ) -> Tuple[GabagoolAction, dict]:
        """Execute a YES buy."""
        shares = self.trade_size / price
        cost = shares * price

        # Update position (actual execution happens in engine)
        position.add_yes_lot(shares, price)
        self.total_trades += 1

        # Try to match after buy
        matched, profit = position.match_shares()
        if matched > 0:
            self.total_profit_locked += profit

        return GabagoolAction.BUY_YES, {
            'side': 'YES',
            'shares': shares,
            'price': price,
            'cost': cost,
            'unmatched_yes': position.unmatched_yes_shares,
            'unmatched_no': position.unmatched_no_shares,
            'matched': matched,
            'locked_profit': profit,
        }

    def _execute_buy_no(
        self,
        position: DipBuyPosition,
        price: float,
    ) -> Tuple[GabagoolAction, dict]:
        """Execute a NO buy."""
        shares = self.trade_size / price
        cost = shares * price

        # Update position (actual execution happens in engine)
        position.add_no_lot(shares, price)
        self.total_trades += 1

        # Try to match after buy
        matched, profit = position.match_shares()
        if matched > 0:
            self.total_profit_locked += profit

        return GabagoolAction.BUY_NO, {
            'side': 'NO',
            'shares': shares,
            'price': price,
            'cost': cost,
            'unmatched_yes': position.unmatched_yes_shares,
            'unmatched_no': position.unmatched_no_shares,
            'matched': matched,
            'locked_profit': profit,
        }

    def _check_close_unmatched(
        self,
        position: DipBuyPosition,
        yes_price: float,
        no_price: float,
    ) -> Tuple[GabagoolAction, Optional[dict]]:
        """Check if we need to close unmatched positions before expiry."""
        # Sell unmatched YES first (if any)
        if position.unmatched_yes_shares > 0.01:
            shares = position.unmatched_yes_shares
            return GabagoolAction.SELL_YES, {
                'side': 'YES',
                'shares': shares,
                'price': yes_price,
                'proceeds': shares * yes_price,
                'reason': 'close_before_expiry',
            }

        # Then sell unmatched NO (if any)
        if position.unmatched_no_shares > 0.01:
            shares = position.unmatched_no_shares
            return GabagoolAction.SELL_NO, {
                'side': 'NO',
                'shares': shares,
                'price': no_price,
                'proceeds': shares * no_price,
                'reason': 'close_before_expiry',
            }

        return GabagoolAction.HOLD, None

    def get_position_summary(self, market_id: str) -> Optional[dict]:
        """Get position summary for a market."""
        if market_id not in self.positions:
            return None

        pos = self.positions[market_id]

        # Calculate pair economics for visualization
        avg_yes_price = pos.matched_cost_yes / pos.matched_shares if pos.matched_shares > 0 else 0
        avg_no_price = pos.matched_cost_no / pos.matched_shares if pos.matched_shares > 0 else 0
        combined_cost = avg_yes_price + avg_no_price
        profit_margin = (1 - combined_cost) if pos.matched_shares > 0 else 0

        return {
            'market_id': market_id,
            'asset': pos.asset,
            'unmatched_yes_shares': pos.unmatched_yes_shares,
            'unmatched_no_shares': pos.unmatched_no_shares,
            'unmatched_yes_cost': pos.unmatched_yes_cost,
            'unmatched_no_cost': pos.unmatched_no_cost,
            'matched_shares': pos.matched_shares,
            'matched_cost_yes': pos.matched_cost_yes,
            'matched_cost_no': pos.matched_cost_no,
            'locked_profit': pos.locked_profit,
            'total_cost': pos.total_cost,
            'unrealized_risk': pos.unrealized_risk,
            'num_trades': len(pos.trades),
            # Pair economics for dashboard visualization
            'avg_yes_price': avg_yes_price,
            'avg_no_price': avg_no_price,
            'combined_cost': combined_cost,
            'profit_margin': profit_margin,
        }

    def get_total_locked_profit(self) -> float:
        """Total locked profit across all markets."""
        return sum(pos.locked_profit for pos in self.positions.values())

    def get_total_cost(self) -> float:
        """Total cost across all markets."""
        return sum(pos.total_cost for pos in self.positions.values())

    def get_total_unrealized_risk(self) -> float:
        """Total unmatched cost (at risk) across all markets."""
        return sum(pos.unrealized_risk for pos in self.positions.values())

    def clear_expired_market(self, market_id: str) -> Optional[dict]:
        """Clear position for an expired market."""
        if market_id not in self.positions:
            return None

        pos = self.positions[market_id]
        summary = self.get_position_summary(market_id)

        # Final P&L = locked profit (guaranteed)
        # Note: unmatched positions either won or lost at resolution
        summary['final_pnl'] = pos.locked_profit
        summary['payout'] = pos.matched_shares  # $1 per matched share

        del self.positions[market_id]
        return summary

    def get_stats(self) -> dict:
        """Get overall strategy stats."""
        return {
            'total_trades': self.total_trades,
            'total_profit_locked': self.total_profit_locked,
            'total_cost': self.get_total_cost(),
            'total_unrealized_risk': self.get_total_unrealized_risk(),
            'active_markets': len(self.positions),
            'positions': {
                mid: self.get_position_summary(mid)
                for mid in self.positions.keys()
            },
        }


# Backwards compatibility
ArbitragePosition = DipBuyPosition
GabagoolPosition = DipBuyPosition


if __name__ == "__main__":
    # Test the strategy
    strategy = GabagoolStrategy(
        yes_buy_threshold=0.48,
        no_buy_threshold=0.48,
        trade_size=10.0,
    )

    market_id = "test_market"
    asset = "BTC"

    print("=== Testing v4 Dip-Buy Strategy ===\n")

    # Simulate price updates
    time_left = timedelta(minutes=10)

    # No dip yet
    action, info = strategy.on_price_update(market_id, asset, 0.52, 0.50, time_left)
    print(f"Prices: YES=52¢ NO=50¢ -> {action.value}")

    # YES dips below threshold
    action, info = strategy.on_price_update(market_id, asset, 0.45, 0.52, time_left)
    print(f"Prices: YES=45¢ NO=52¢ -> {action.value}")
    if info:
        print(f"  Bought {info['shares']:.2f} YES @ {info['price']:.2f}")

    # Wait a bit (simulate cooldown passing)
    import time
    pos = strategy.positions[market_id]
    pos.last_yes_trade_time = None  # Reset for test

    # NO dips - should trigger matching
    action, info = strategy.on_price_update(market_id, asset, 0.50, 0.46, time_left)
    print(f"\nPrices: YES=50¢ NO=46¢ -> {action.value}")
    if info:
        print(f"  Bought {info['shares']:.2f} NO @ {info['price']:.2f}")

    # Check position
    print("\n=== Position Summary ===")
    summary = strategy.get_position_summary(market_id)
    if summary:
        print(f"  Matched shares: {summary['matched_shares']:.2f}")
        print(f"  Locked profit: ${summary['locked_profit']:.2f}")
        print(f"  Unmatched YES: {summary['unmatched_yes_shares']:.2f}")
        print(f"  Unmatched NO: {summary['unmatched_no_shares']:.2f}")
        print(f"  Total cost: ${summary['total_cost']:.2f}")
