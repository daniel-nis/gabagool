"""
Late-Game Snipe Engine.

Core logic for identifying and executing snipe opportunities
in the final seconds of 15-min crypto binary markets.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass

import sys
sys.path.insert(0, '..')

from helpers.polymarket_api import Market, get_15m_markets
from helpers.orderbook_wss import OrderbookStreamer, OrderbookState
from helpers.price_feed import get_price_with_history, PriceState

from sniper.models import SnipeTrade, SnipeOpportunity, TradeStore
from sniper import config


@dataclass
class MarketState:
    """Current state of a market being monitored."""
    market: Market
    time_remaining: float
    up_price: float
    down_price: float
    up_spread: float
    down_spread: float
    in_snipe_zone: bool
    leader: Optional[str] = None
    leader_price: Optional[float] = None
    expected_roi: Optional[float] = None
    can_enter: bool = False
    skip_reason: Optional[str] = None
    price_state: Optional[PriceState] = None
    price_confirms: Optional[bool] = None


class SnipeEngine:
    """
    Late-Game Snipe Engine.

    Monitors 15-min markets and executes snipe trades when conditions are met:
    - Time remaining: 10-60 seconds
    - Leader price: 70-92%
    - Expected ROI: 8%+
    - Optional: Binance confirms direction
    """

    def __init__(self, trade_store: TradeStore = None):
        self.store = trade_store or TradeStore(config.DB_PATH)
        self.orderbook = OrderbookStreamer()

        # Current state
        self.markets: Dict[str, Market] = {}
        self.market_states: Dict[str, MarketState] = {}
        self.price_states: Dict[str, PriceState] = {}  # Asset -> current price state

        # Tracking
        self.active_trades: Dict[str, SnipeTrade] = {}  # market_id -> trade
        self.traded_markets: set = set()  # Markets we've already traded (one shot)

        # Callbacks
        self._on_update: List[Callable] = []
        self._on_trade: List[Callable] = []
        self._on_opportunity: List[Callable] = []

        self.running = False

    def on_update(self, callback: Callable):
        """Register callback for state updates."""
        self._on_update.append(callback)

    def on_trade(self, callback: Callable):
        """Register callback for trade executions."""
        self._on_trade.append(callback)

    def on_opportunity(self, callback: Callable):
        """Register callback for opportunities (entered or skipped)."""
        self._on_opportunity.append(callback)

    def _emit_update(self):
        """Notify listeners of state change."""
        for cb in self._on_update:
            try:
                cb(self.market_states)
            except Exception as e:
                print(f"Update callback error: {e}")

    def _emit_trade(self, trade: SnipeTrade):
        """Notify listeners of trade execution."""
        for cb in self._on_trade:
            try:
                cb(trade)
            except Exception as e:
                print(f"Trade callback error: {e}")

    def _emit_opportunity(self, opp: SnipeOpportunity):
        """Notify listeners of opportunity."""
        for cb in self._on_opportunity:
            try:
                cb(opp)
            except Exception as e:
                print(f"Opportunity callback error: {e}")

    def get_market_state(self, condition_id: str) -> Optional[MarketState]:
        """Get current state of a market."""
        return self.market_states.get(condition_id)

    def get_all_states(self) -> Dict[str, MarketState]:
        """Get all market states."""
        return self.market_states.copy()

    def get_stats(self) -> dict:
        """Get trading statistics."""
        return self.store.get_stats()

    def get_recent_trades(self, limit: int = 20) -> List[SnipeTrade]:
        """Get recent trades."""
        return self.store.get_all_trades(limit)

    def evaluate_entry(self, market: Market, now: datetime) -> MarketState:
        """
        Evaluate if we should enter a snipe position.

        Returns MarketState with entry decision and reasoning.
        """
        time_remaining = (market.end_time - now).total_seconds()

        # Get orderbook prices
        ob_up = self.orderbook.get_orderbook(market.condition_id, "UP")
        ob_down = self.orderbook.get_orderbook(market.condition_id, "DOWN")

        # Use mid prices from orderbook, fallback to API prices
        if ob_up and ob_up.mid_price:
            up_price = ob_up.mid_price
            up_spread = ob_up.spread or 0.0
        else:
            up_price = market.price_up
            up_spread = 0.0

        if ob_down and ob_down.mid_price:
            down_price = ob_down.mid_price
            down_spread = ob_down.spread or 0.0
        else:
            down_price = market.price_down
            down_spread = 0.0

        # Determine snipe zone
        in_snipe_zone = config.MIN_TIME_REMAINING <= time_remaining <= config.MAX_TIME_REMAINING

        state = MarketState(
            market=market,
            time_remaining=time_remaining,
            up_price=up_price,
            down_price=down_price,
            up_spread=up_spread,
            down_spread=down_spread,
            in_snipe_zone=in_snipe_zone,
        )

        # Quick exit if not in snipe zone
        if not in_snipe_zone:
            if time_remaining > config.MAX_TIME_REMAINING:
                state.skip_reason = f"Too early ({time_remaining:.0f}s left)"
            else:
                state.skip_reason = f"Too late ({time_remaining:.0f}s left)"
            return state

        # Already traded this market?
        if market.condition_id in self.traded_markets:
            state.skip_reason = "Already traded"
            return state

        # Identify leader
        if up_price > down_price:
            leader, leader_price = "UP", up_price
            leader_spread = up_spread
        else:
            leader, leader_price = "DOWN", down_price
            leader_spread = down_spread

        state.leader = leader
        state.leader_price = leader_price

        # Check leader price bounds
        if leader_price < config.MIN_LEADER_PRICE:
            state.skip_reason = f"Leader too weak ({leader_price:.1%})"
            return state

        if leader_price > config.MAX_LEADER_PRICE:
            state.skip_reason = f"Leader too strong ({leader_price:.1%})"
            return state

        # Calculate expected ROI
        expected_roi = (1.0 - leader_price) / leader_price
        state.expected_roi = expected_roi

        if expected_roi < config.MIN_EXPECTED_ROI:
            state.skip_reason = f"ROI too low ({expected_roi:.1%})"
            return state

        # Check spread (illiquidity filter)
        if leader_spread > config.MAX_SPREAD:
            state.skip_reason = f"Spread too wide ({leader_spread:.1%})"
            return state

        # Price confirmation (optional)
        if config.REQUIRE_BINANCE_CONFIRMATION:
            price_state = self.price_states.get(market.asset)
            state.price_state = price_state

            if price_state and price_state.direction:
                # Does Polymarket leader match spot price direction?
                if leader == price_state.direction:
                    state.price_confirms = True
                else:
                    state.price_confirms = False
                    state.skip_reason = f"Spot price disagrees ({price_state.direction})"
                    return state
            else:
                # No price data, skip confirmation
                state.price_confirms = None

        # All checks passed - can enter
        state.can_enter = True
        return state

    def execute_paper_trade(self, state: MarketState) -> SnipeTrade:
        """Execute a paper trade based on market state."""
        now = datetime.now(timezone.utc)

        # Calculate shares
        shares = config.TRADE_SIZE / state.leader_price
        cost = config.TRADE_SIZE

        trade = SnipeTrade(
            market_id=state.market.condition_id,
            asset=state.market.asset,
            side=state.leader,
            entry_price=state.leader_price,
            entry_time=now,
            shares=shares,
            cost=cost,
            time_remaining_at_entry=state.time_remaining,
            binance_confirmed=state.price_confirms,
        )

        # Save to DB
        self.store.save_trade(trade)

        # Track
        self.active_trades[state.market.condition_id] = trade
        self.traded_markets.add(state.market.condition_id)

        print(f"\n{'='*50}")
        print(f"SNIPE EXECUTED: {state.market.asset}")
        print(f"  Side: {trade.side}")
        print(f"  Entry: {trade.entry_price:.1%}")
        print(f"  Shares: {trade.shares:.2f}")
        print(f"  Cost: ${trade.cost:.2f}")
        print(f"  Time left: {state.time_remaining:.0f}s")
        print(f"  Expected ROI: {state.expected_roi:.1%}")
        print(f"{'='*50}\n")

        self._emit_trade(trade)
        return trade

    def resolve_trade(self, trade: SnipeTrade, winning_side: str):
        """Resolve a trade after market closes."""
        if trade.side == winning_side:
            exit_price = 1.0
            pnl = trade.shares * 1.0 - trade.cost
        else:
            exit_price = 0.0
            pnl = -trade.cost

        roi = pnl / trade.cost

        # Update in DB
        self.store.resolve_trade(trade.id, exit_price, pnl, roi)

        # Update local
        trade.resolved = True
        trade.exit_price = exit_price
        trade.pnl = pnl
        trade.roi = roi

        result = "WIN" if exit_price == 1.0 else "LOSS"
        print(f"\n{'='*50}")
        print(f"TRADE RESOLVED: {trade.asset} - {result}")
        print(f"  Side: {trade.side}")
        print(f"  Entry: {trade.entry_price:.1%}")
        print(f"  P&L: ${trade.pnl:+.2f}")
        print(f"  ROI: {trade.roi:+.1%}")
        print(f"{'='*50}\n")

        self._emit_trade(trade)

    async def _refresh_markets(self):
        """Periodically refresh market list."""
        while self.running:
            try:
                markets = get_15m_markets(config.ASSETS)
                now = datetime.now(timezone.utc)

                # Update markets dict
                active_ids = set()
                for m in markets:
                    self.markets[m.condition_id] = m
                    active_ids.add(m.condition_id)

                    # Subscribe to orderbook if new
                    if m.condition_id not in [s[0] for s in self.orderbook._subscriptions]:
                        self.orderbook.subscribe(m.condition_id, m.token_up, m.token_down)

                # Clean up expired markets
                expired = [cid for cid, m in self.markets.items()
                           if m.end_time <= now or cid not in active_ids]
                for cid in expired:
                    # Check for unresolved trades
                    if cid in self.active_trades:
                        trade = self.active_trades[cid]
                        if not trade.resolved:
                            # Determine winner (price > 0.5 = win)
                            ob_up = self.orderbook.get_orderbook(cid, "UP")
                            if ob_up and ob_up.mid_price:
                                winning_side = "UP" if ob_up.mid_price > 0.5 else "DOWN"
                            else:
                                # Fallback - use last known leader
                                state = self.market_states.get(cid)
                                winning_side = state.leader if state else "UP"

                            self.resolve_trade(trade, winning_side)
                        del self.active_trades[cid]

                    if cid in self.markets:
                        del self.markets[cid]
                    if cid in self.market_states:
                        del self.market_states[cid]

                # Clear stale orderbooks
                self.orderbook.clear_stale(active_ids)

                if markets:
                    print(f"[Markets] {len(markets)} active: " +
                          ", ".join(f"{m.asset}" for m in markets))

            except Exception as e:
                print(f"Market refresh error: {e}")

            await asyncio.sleep(config.MARKET_REFRESH_INTERVAL)

    async def _refresh_prices(self):
        """Periodically refresh spot price data."""
        while self.running:
            try:
                for asset in config.ASSETS:
                    state = get_price_with_history(asset)
                    if state:
                        self.price_states[asset] = state

            except Exception as e:
                print(f"Price refresh error: {e}")

            await asyncio.sleep(15)  # Every 15 seconds (CoinGecko rate limit friendly)

    async def _tick(self):
        """Main evaluation loop."""
        while self.running:
            now = datetime.now(timezone.utc)

            for condition_id, market in list(self.markets.items()):
                # Evaluate entry conditions
                state = self.evaluate_entry(market, now)
                self.market_states[condition_id] = state

                # Log opportunity if in snipe zone
                if state.in_snipe_zone and condition_id not in self.traded_markets:
                    opp = SnipeOpportunity(
                        market_id=condition_id,
                        asset=market.asset,
                        timestamp=now,
                        time_remaining=state.time_remaining,
                        up_price=state.up_price,
                        down_price=state.down_price,
                        leader=state.leader or "NONE",
                        leader_price=state.leader_price or 0.0,
                        spread=state.up_spread if state.leader == "UP" else state.down_spread,
                        entered=state.can_enter,
                        skip_reason=state.skip_reason,
                        binance_price=state.price_state.price if state.price_state else None,
                        binance_direction=state.price_state.direction if state.price_state else None,
                        binance_confirms=state.price_confirms,
                    )

                    # Save every opportunity in snipe zone for analysis
                    self.store.save_opportunity(opp)
                    self._emit_opportunity(opp)

                    # Execute if conditions met
                    if state.can_enter:
                        self.execute_paper_trade(state)

            # Emit state update
            self._emit_update()

            await asyncio.sleep(config.TICK_INTERVAL)

    async def run(self):
        """Start the snipe engine."""
        self.running = True
        print("\n" + "="*60)
        print("LATE-GAME SNIPER STARTED")
        print("="*60)
        print(f"Assets: {config.ASSETS}")
        print(f"Snipe window: {config.MIN_TIME_REMAINING}-{config.MAX_TIME_REMAINING}s")
        print(f"Leader range: {config.MIN_LEADER_PRICE:.0%}-{config.MAX_LEADER_PRICE:.0%}")
        print(f"Min ROI: {config.MIN_EXPECTED_ROI:.0%}")
        print(f"Trade size: ${config.TRADE_SIZE}")
        print(f"Binance confirmation: {config.REQUIRE_BINANCE_CONFIRMATION}")
        print("="*60 + "\n")

        # Load any unresolved trades
        for trade in self.store.get_unresolved_trades():
            self.active_trades[trade.market_id] = trade
            self.traded_markets.add(trade.market_id)

        await asyncio.gather(
            self.orderbook.stream(),
            self._refresh_markets(),
            self._refresh_prices(),
            self._tick(),
        )

    def stop(self):
        """Stop the snipe engine."""
        self.running = False
        self.orderbook.stop()


if __name__ == "__main__":
    # Quick test
    engine = SnipeEngine()
    asyncio.run(engine.run())
