"""
Risk Manager for Gabagool Live Trading.

Implements safeguards to protect against excessive losses:
- Minimum balance check before trading
- Daily loss limit
- Max position per market
- Circuit breaker (consecutive losses)
- Max total exposure

Usage:
    from helpers.risk_manager import RiskManager, RiskConfig

    config = RiskConfig(daily_loss_limit=100.0)
    risk = RiskManager(config)

    # Before each trade:
    allowed, reason = risk.can_trade(
        market_id="btc-15min",
        side="YES",
        amount=10.0,
        current_balance=500.0,
    )
    if not allowed:
        print(f"Trade blocked: {reason}")

    # After each trade:
    risk.record_trade(market_id, side, amount, pnl)
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List
from collections import defaultdict
from datetime import datetime, timezone


@dataclass
class RiskConfig:
    """Risk management configuration."""

    # Minimum balance required to trade
    min_balance: float = 50.0

    # Maximum loss allowed per day (resets at midnight UTC)
    daily_loss_limit: float = 100.0

    # Maximum position (cost) per market
    max_position_per_market: float = 50.0

    # Maximum total exposure across all markets
    max_total_exposure: float = 200.0

    # Circuit breaker: pause after N consecutive losses
    consecutive_loss_limit: int = 3

    # Circuit breaker cooldown in seconds
    circuit_breaker_cooldown: float = 300.0  # 5 minutes

    # Maximum single trade size
    max_trade_size: float = 25.0

    # Minimum time between trades (seconds)
    min_trade_interval: float = 1.0


@dataclass
class MarketPosition:
    """Track position in a single market."""
    market_id: str
    yes_cost: float = 0.0
    no_cost: float = 0.0
    yes_shares: float = 0.0
    no_shares: float = 0.0
    matched_profit: float = 0.0
    last_trade_time: float = 0.0

    @property
    def total_cost(self) -> float:
        """Total cost (exposure) in this market."""
        return self.yes_cost + self.no_cost

    @property
    def unmatched_cost(self) -> float:
        """Unmatched (at-risk) cost."""
        matched = min(self.yes_cost, self.no_cost)
        return self.total_cost - (2 * matched)


@dataclass
class DailyStats:
    """Track daily trading statistics."""
    date: str  # YYYY-MM-DD
    realized_pnl: float = 0.0
    trade_count: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def win_rate(self) -> float:
        """Win rate as percentage."""
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0


class RiskManager:
    """
    Risk management for live trading.

    Enforces position limits, loss limits, and circuit breakers.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        """Initialize risk manager with configuration."""
        self.config = config or RiskConfig()

        # Track positions by market
        self.positions: Dict[str, MarketPosition] = {}

        # Daily stats (keyed by date string)
        self.daily_stats: Dict[str, DailyStats] = {}

        # Circuit breaker state
        self.consecutive_losses: int = 0
        self.circuit_breaker_until: float = 0.0

        # Trade timing
        self.last_trade_time: float = 0.0

        # Trade history for analysis
        self.trade_history: List[Dict] = []

    def _get_today(self) -> str:
        """Get today's date in UTC as string."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_daily_stats(self) -> DailyStats:
        """Get or create today's stats."""
        today = self._get_today()
        if today not in self.daily_stats:
            self.daily_stats[today] = DailyStats(date=today)
        return self.daily_stats[today]

    def _get_position(self, market_id: str) -> MarketPosition:
        """Get or create position for market."""
        if market_id not in self.positions:
            self.positions[market_id] = MarketPosition(market_id=market_id)
        return self.positions[market_id]

    def get_total_exposure(self) -> float:
        """Get total exposure across all markets."""
        return sum(p.total_cost for p in self.positions.values())

    def get_total_unmatched(self) -> float:
        """Get total unmatched (at-risk) exposure."""
        return sum(p.unmatched_cost for p in self.positions.values())

    def can_trade(
        self,
        market_id: str,
        side: str,  # "YES" or "NO"
        amount: float,
        current_balance: float,
    ) -> Tuple[bool, str]:
        """
        Check if a trade is allowed.

        Args:
            market_id: Market identifier
            side: "YES" or "NO"
            amount: Trade amount in dollars
            current_balance: Current wallet balance

        Returns:
            (allowed, reason) tuple
        """
        now = time.time()

        # Check 1: Circuit breaker
        if now < self.circuit_breaker_until:
            remaining = int(self.circuit_breaker_until - now)
            return False, f"Circuit breaker active ({remaining}s remaining)"

        # Check 2: Minimum balance
        if current_balance < self.config.min_balance:
            return False, f"Balance ${current_balance:.2f} below minimum ${self.config.min_balance:.2f}"

        # Check 3: Trade size limit
        if amount > self.config.max_trade_size:
            return False, f"Trade ${amount:.2f} exceeds max ${self.config.max_trade_size:.2f}"

        # Check 4: Sufficient balance for trade
        if amount > current_balance:
            return False, f"Insufficient balance (${current_balance:.2f}) for ${amount:.2f} trade"

        # Check 5: Daily loss limit
        daily = self._get_daily_stats()
        if daily.realized_pnl < -self.config.daily_loss_limit:
            return False, f"Daily loss limit reached (${-daily.realized_pnl:.2f} lost)"

        # Check 6: Position limit per market
        position = self._get_position(market_id)
        if position.total_cost + amount > self.config.max_position_per_market:
            return False, f"Market position limit ${self.config.max_position_per_market:.2f} would be exceeded"

        # Check 7: Total exposure limit
        total_exposure = self.get_total_exposure()
        if total_exposure + amount > self.config.max_total_exposure:
            return False, f"Total exposure limit ${self.config.max_total_exposure:.2f} would be exceeded"

        # Check 8: Trade interval
        if now - self.last_trade_time < self.config.min_trade_interval:
            return False, f"Trade interval too short ({self.config.min_trade_interval}s required)"

        # All checks passed
        return True, "OK"

    def record_trade(
        self,
        market_id: str,
        side: str,
        amount: float,
        shares: float,
        price: float,
        pnl: float = 0.0,
        is_match: bool = False,
    ) -> None:
        """
        Record a completed trade.

        Args:
            market_id: Market identifier
            side: "YES" or "NO"
            amount: Trade amount in dollars
            shares: Number of shares traded
            price: Execution price
            pnl: Realized P&L (for matched trades)
            is_match: Whether this closes a matched position
        """
        now = time.time()
        position = self._get_position(market_id)
        daily = self._get_daily_stats()

        # Update position
        if side.upper() == "YES":
            position.yes_cost += amount
            position.yes_shares += shares
        else:
            position.no_cost += amount
            position.no_shares += shares
        position.last_trade_time = now

        # Update daily stats
        daily.trade_count += 1

        # Record P&L for matched trades
        if is_match:
            daily.realized_pnl += pnl
            position.matched_profit += pnl

            if pnl > 0:
                daily.wins += 1
                self.consecutive_losses = 0
            elif pnl < 0:
                daily.losses += 1
                self.consecutive_losses += 1

                # Check circuit breaker
                if self.consecutive_losses >= self.config.consecutive_loss_limit:
                    self.circuit_breaker_until = now + self.config.circuit_breaker_cooldown
                    print(f"[RISK] Circuit breaker triggered after {self.consecutive_losses} consecutive losses")

        # Update last trade time
        self.last_trade_time = now

        # Record in history
        self.trade_history.append({
            "timestamp": now,
            "market_id": market_id,
            "side": side,
            "amount": amount,
            "shares": shares,
            "price": price,
            "pnl": pnl,
            "is_match": is_match,
        })

    def record_settlement(
        self,
        market_id: str,
        pnl: float,
        winning_side: Optional[str] = None,
    ) -> None:
        """
        Record market settlement.

        Args:
            market_id: Market identifier
            pnl: Realized P&L from settlement
            winning_side: Which side won ("YES" or "NO")
        """
        daily = self._get_daily_stats()
        daily.realized_pnl += pnl

        if pnl > 0:
            daily.wins += 1
            self.consecutive_losses = 0
        elif pnl < 0:
            daily.losses += 1
            self.consecutive_losses += 1

        # Clear position
        if market_id in self.positions:
            del self.positions[market_id]

    def reset_market(self, market_id: str) -> None:
        """Reset position for a market (e.g., on new 15-min period)."""
        if market_id in self.positions:
            del self.positions[market_id]

    def get_status(self) -> Dict:
        """Get current risk status for dashboard."""
        daily = self._get_daily_stats()
        now = time.time()

        return {
            "total_exposure": self.get_total_exposure(),
            "max_exposure": self.config.max_total_exposure,
            "exposure_pct": (self.get_total_exposure() / self.config.max_total_exposure) * 100,
            "daily_pnl": daily.realized_pnl,
            "daily_loss_limit": self.config.daily_loss_limit,
            "daily_trades": daily.trade_count,
            "win_rate": daily.win_rate,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker_active": now < self.circuit_breaker_until,
            "circuit_breaker_remaining": max(0, int(self.circuit_breaker_until - now)),
            "positions": {
                mid: {
                    "yes_cost": p.yes_cost,
                    "no_cost": p.no_cost,
                    "total_cost": p.total_cost,
                    "matched_profit": p.matched_profit,
                }
                for mid, p in self.positions.items()
            },
        }

    def check_health(self) -> Tuple[bool, List[str]]:
        """
        Check overall risk health.

        Returns:
            (healthy, warnings) tuple
        """
        warnings = []
        daily = self._get_daily_stats()

        # Check exposure level
        exposure_pct = self.get_total_exposure() / self.config.max_total_exposure
        if exposure_pct > 0.8:
            warnings.append(f"High exposure: {exposure_pct*100:.0f}% of limit")

        # Check daily P&L
        loss_pct = abs(daily.realized_pnl) / self.config.daily_loss_limit if daily.realized_pnl < 0 else 0
        if loss_pct > 0.7:
            warnings.append(f"Approaching daily loss limit: {loss_pct*100:.0f}%")

        # Check consecutive losses
        if self.consecutive_losses >= 2:
            warnings.append(f"Consecutive losses: {self.consecutive_losses}")

        # Check win rate
        if daily.trade_count >= 10 and daily.win_rate < 30:
            warnings.append(f"Low win rate: {daily.win_rate:.0f}%")

        healthy = len(warnings) == 0
        return healthy, warnings


# Singleton for global access
_risk_manager: Optional[RiskManager] = None


def get_risk_manager(config: Optional[RiskConfig] = None) -> RiskManager:
    """Get or create the global risk manager."""
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager(config)
    return _risk_manager


def reset_risk_manager() -> None:
    """Reset the global risk manager (for testing)."""
    global _risk_manager
    _risk_manager = None


if __name__ == "__main__":
    # Test the risk manager
    print("Risk Manager Test\n")

    config = RiskConfig(
        daily_loss_limit=100.0,
        max_position_per_market=50.0,
        max_total_exposure=200.0,
        consecutive_loss_limit=3,
    )
    risk = RiskManager(config)

    # Test basic trade
    allowed, reason = risk.can_trade(
        market_id="btc-15min",
        side="YES",
        amount=10.0,
        current_balance=500.0,
    )
    print(f"Trade 1: {allowed} - {reason}")

    # Record the trade
    risk.record_trade("btc-15min", "YES", 10.0, 22.0, 0.45)

    # Test position limit
    allowed, reason = risk.can_trade(
        market_id="btc-15min",
        side="YES",
        amount=50.0,
        current_balance=500.0,
    )
    print(f"Trade 2 (over position limit): {allowed} - {reason}")

    # Simulate consecutive losses
    for i in range(3):
        risk.record_trade("btc-15min", "NO", 10.0, 22.0, 0.45, pnl=-2.0, is_match=True)

    # Check circuit breaker
    allowed, reason = risk.can_trade(
        market_id="btc-15min",
        side="YES",
        amount=10.0,
        current_balance=500.0,
    )
    print(f"Trade 3 (after circuit breaker): {allowed} - {reason}")

    # Print status
    print(f"\nStatus: {risk.get_status()}")

    # Check health
    healthy, warnings = risk.check_health()
    print(f"\nHealth: {'OK' if healthy else 'WARNING'}")
    for w in warnings:
        print(f"  - {w}")
