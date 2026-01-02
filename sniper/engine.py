"""
Late-Game Snipe Engine.

Core logic for identifying and executing snipe opportunities
in the final seconds of 15-min crypto binary markets.
"""
import asyncio
import csv
import os
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, asdict

import sys
sys.path.insert(0, '..')

from helpers.polymarket_api import Market, get_15m_markets
from helpers.orderbook_wss import OrderbookStreamer, OrderbookState

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


@dataclass
class Observation:
    """An observation logged in observation mode."""
    timestamp: str
    market_id: str
    asset: str
    time_remaining: float
    leader: str
    leader_price: float
    up_price: float
    down_price: float
    spread: float
    would_enter: bool
    skip_reason: Optional[str]
    # Filled after resolution
    outcome: Optional[str] = None  # "UP" or "DOWN"
    would_have_won: Optional[bool] = None
    potential_roi: Optional[float] = None


class SnipeEngine:
    """
    Late-Game Snipe Engine.

    Monitors 15-min Polymarket markets and executes snipe trades when conditions are met.
    Reads parameters from config.settings for hot-reload support.
    """

    def __init__(self, trade_store: TradeStore = None):
        self.store = trade_store or TradeStore(config.DB_PATH)
        self.orderbook = OrderbookStreamer()

        # Current state
        self.markets: Dict[str, Market] = {}
        self.market_states: Dict[str, MarketState] = {}

        # Tracking
        self.active_trades: Dict[str, SnipeTrade] = {}  # market_id -> trade
        self.traded_markets: set = set()  # Markets we've already traded (one shot)

        # Observation mode tracking
        self.pending_observations: Dict[str, Observation] = {}  # market_id -> observation
        self._init_observation_csv()

        # Callbacks
        self._on_update: List[Callable] = []
        self._on_trade: List[Callable] = []
        self._on_opportunity: List[Callable] = []

        self.running = False

    def _init_observation_csv(self):
        """Initialize observation CSV file with headers if needed."""
        os.makedirs(os.path.dirname(config.OBSERVATION_CSV), exist_ok=True)
        if not os.path.exists(config.OBSERVATION_CSV):
            with open(config.OBSERVATION_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_id', 'asset', 'time_remaining',
                    'leader', 'leader_price', 'up_price', 'down_price', 'spread',
                    'would_enter', 'skip_reason',
                    'outcome', 'would_have_won', 'potential_roi'
                ])

    def _log_observation(self, obs: Observation):
        """Append observation to CSV."""
        with open(config.OBSERVATION_CSV, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                obs.timestamp, obs.market_id, obs.asset, f"{obs.time_remaining:.1f}",
                obs.leader, f"{obs.leader_price:.4f}", f"{obs.up_price:.4f}",
                f"{obs.down_price:.4f}", f"{obs.spread:.4f}",
                obs.would_enter, obs.skip_reason or "",
                obs.outcome or "", obs.would_have_won if obs.would_have_won is not None else "",
                f"{obs.potential_roi:.4f}" if obs.potential_roi is not None else ""
            ])

    def _resolve_observation(self, market_id: str, winning_side: str):
        """Resolve a pending observation with outcome."""
        if market_id not in self.pending_observations:
            return

        obs = self.pending_observations.pop(market_id)
        obs.outcome = winning_side
        obs.would_have_won = (obs.leader == winning_side)

        if obs.would_have_won:
            obs.potential_roi = (1.0 - obs.leader_price) / obs.leader_price
        else:
            obs.potential_roi = -1.0  # Lost entire stake

        self._log_observation(obs)

        result = "WIN" if obs.would_have_won else "LOSS"
        print(f"[OBS] {obs.asset} resolved: {result} (would have been {obs.potential_roi:+.1%})")

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
        stats = self.store.get_stats()
        stats['observation_mode'] = config.settings.observation_mode
        stats['pending_observations'] = len(self.pending_observations)
        return stats

    def get_recent_trades(self, limit: int = 20) -> List[SnipeTrade]:
        """Get recent trades."""
        return self.store.get_all_trades(limit)

    def get_settings(self) -> dict:
        """Get current settings as dict."""
        return asdict(config.settings)

    def update_settings(self, **kwargs):
        """Update settings (hot reload)."""
        config.settings.update(**kwargs)
        print(f"[Settings] Updated: {kwargs}")

    def evaluate_entry(self, market: Market, now: datetime) -> MarketState:
        """
        Evaluate if we should enter a snipe position.
        Reads thresholds from config.settings for hot-reload support.
        """
        s = config.settings  # Shorthand for current settings
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

        # Determine snipe zone using current settings
        in_snipe_zone = s.min_time_remaining <= time_remaining <= s.max_time_remaining

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
            if time_remaining > s.max_time_remaining:
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
        if leader_price < s.min_leader_price:
            state.skip_reason = f"Leader too weak ({leader_price:.1%})"
            return state

        if leader_price > s.max_leader_price:
            state.skip_reason = f"Leader too strong ({leader_price:.1%})"
            return state

        # Calculate expected ROI
        expected_roi = (1.0 - leader_price) / leader_price
        state.expected_roi = expected_roi

        if expected_roi < s.min_expected_roi:
            state.skip_reason = f"ROI too low ({expected_roi:.1%})"
            return state

        # Check spread (illiquidity filter)
        if leader_spread > s.max_spread:
            state.skip_reason = f"Spread too wide ({leader_spread:.1%})"
            return state

        # All checks passed - can enter
        state.can_enter = True
        return state

    def execute_paper_trade(self, state: MarketState) -> SnipeTrade:
        """Execute a paper trade based on market state."""
        now = datetime.now(timezone.utc)
        s = config.settings

        # Calculate shares
        shares = s.trade_size / state.leader_price
        cost = s.trade_size

        trade = SnipeTrade(
            market_id=state.market.condition_id,
            asset=state.market.asset,
            side=state.leader,
            entry_price=state.leader_price,
            entry_time=now,
            shares=shares,
            cost=cost,
            time_remaining_at_entry=state.time_remaining,
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

    def _get_leader_price(self, market: Market) -> Optional[float]:
        """Get current leader price from orderbook."""
        ob_up = self.orderbook.get_orderbook(market.condition_id, "UP")
        ob_down = self.orderbook.get_orderbook(market.condition_id, "DOWN")

        up_price = ob_up.mid_price if ob_up and ob_up.mid_price else market.price_up
        down_price = ob_down.mid_price if ob_down and ob_down.mid_price else market.price_down

        if up_price is None and down_price is None:
            return None

        return max(up_price or 0, down_price or 0)

    def _exit_trade_early(self, trade: SnipeTrade, exit_price: float, reason: str = "stop-loss"):
        """Exit a trade early at current price."""
        pnl = (trade.shares * exit_price) - trade.cost
        roi = pnl / trade.cost

        # Update in DB
        self.store.resolve_trade(trade.id, exit_price, pnl, roi)

        # Update local
        trade.resolved = True
        trade.exit_price = exit_price
        trade.pnl = pnl
        trade.roi = roi

        # Remove from active trades
        if trade.market_id in self.active_trades:
            del self.active_trades[trade.market_id]

        print(f"\n{'='*50}")
        print(f"STOP-LOSS EXIT: {trade.asset}")
        print(f"  Side: {trade.side}")
        print(f"  Entry: {trade.entry_price:.1%}")
        print(f"  Exit: {exit_price:.1%}")
        print(f"  P&L: ${pnl:+.2f} (saved ${trade.cost - abs(pnl):.2f})")
        print(f"  ROI: {roi:+.1%}")
        print(f"{'='*50}\n")

        self._emit_trade(trade)

    def _monitor_stop_loss(self):
        """Check active positions for stop-loss triggers (dual system)."""
        s = config.settings

        for market_id, trade in list(self.active_trades.items()):
            if trade.resolved:
                continue

            market = self.markets.get(market_id)
            if not market:
                continue

            # Get current leader price from orderbook
            current_leader = self._get_leader_price(market)
            if current_leader is None:
                continue

            # Check ABSOLUTE threshold: exit if leader drops to threshold
            if current_leader <= s.stop_loss_threshold:
                self._exit_trade_early(trade, current_leader, "stop-loss-absolute")
                continue

            # Check RELATIVE drop: exit if price dropped too much from entry
            drop_from_entry = trade.entry_price - current_leader
            if drop_from_entry >= s.stop_loss_drop:
                self._exit_trade_early(trade, current_leader, "stop-loss-relative")

    async def _refresh_markets(self):
        """Periodically refresh market list."""
        while self.running:
            try:
                # Use current settings for assets
                markets = get_15m_markets(config.settings.assets)
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
                    # Determine winner
                    ob_up = self.orderbook.get_orderbook(cid, "UP")
                    if ob_up and ob_up.mid_price:
                        winning_side = "UP" if ob_up.mid_price > 0.5 else "DOWN"
                    else:
                        state = self.market_states.get(cid)
                        winning_side = state.leader if state else "UP"

                    # Resolve any pending observations
                    if cid in self.pending_observations:
                        self._resolve_observation(cid, winning_side)

                    # Check for unresolved trades
                    if cid in self.active_trades:
                        trade = self.active_trades[cid]
                        if not trade.resolved:
                            self.resolve_trade(trade, winning_side)
                        del self.active_trades[cid]

                    if cid in self.markets:
                        del self.markets[cid]
                    if cid in self.market_states:
                        del self.market_states[cid]

                # Clear stale orderbooks
                self.orderbook.clear_stale(active_ids)

                if markets:
                    mode = "OBSERVE" if config.settings.observation_mode else "TRADE"
                    print(f"[Markets] {len(markets)} active ({mode} mode): " +
                          ", ".join(f"{m.asset}" for m in markets))

            except Exception as e:
                print(f"Market refresh error: {e}")

            await asyncio.sleep(config.MARKET_REFRESH_INTERVAL)

    async def _tick(self):
        """Main evaluation loop."""
        while self.running:
            now = datetime.now(timezone.utc)
            s = config.settings

            for condition_id, market in list(self.markets.items()):
                # Evaluate entry conditions
                state = self.evaluate_entry(market, now)
                self.market_states[condition_id] = state

                # Log opportunity if in snipe zone and not already logged
                if state.in_snipe_zone and condition_id not in self.traded_markets:
                    # Check if we already have an observation for this market
                    if condition_id not in self.pending_observations:
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
                            entered=state.can_enter and not s.observation_mode,
                            skip_reason=state.skip_reason if not state.can_enter else ("Observation mode" if s.observation_mode else None),
                        )

                        # Save to DB
                        self.store.save_opportunity(opp)
                        self._emit_opportunity(opp)

                        # If observation mode, log to CSV and track for resolution
                        if s.observation_mode and state.can_enter:
                            obs = Observation(
                                timestamp=now.isoformat(),
                                market_id=condition_id,
                                asset=market.asset,
                                time_remaining=state.time_remaining,
                                leader=state.leader,
                                leader_price=state.leader_price,
                                up_price=state.up_price,
                                down_price=state.down_price,
                                spread=state.up_spread if state.leader == "UP" else state.down_spread,
                                would_enter=True,
                                skip_reason=None,
                            )
                            self.pending_observations[condition_id] = obs
                            print(f"[OBS] {market.asset}: Would enter {state.leader} @ {state.leader_price:.1%} "
                                  f"(ROI: {state.expected_roi:.1%}, {state.time_remaining:.0f}s left)")

                    # Execute if conditions met AND not in observation mode
                    if state.can_enter and not s.observation_mode:
                        self.execute_paper_trade(state)

            # Monitor active positions for stop-loss
            self._monitor_stop_loss()

            # Emit state update
            self._emit_update()

            await asyncio.sleep(config.TICK_INTERVAL)

    async def run(self):
        """Start the snipe engine."""
        self.running = True
        s = config.settings

        print("\n" + "="*60)
        print("LATE-GAME SNIPER STARTED")
        print("="*60)
        print(f"Mode: {'OBSERVATION' if s.observation_mode else 'TRADING'}")
        print(f"Assets: {s.assets}")
        print(f"Snipe window: {s.min_time_remaining}-{s.max_time_remaining}s")
        print(f"Leader range: {s.min_leader_price:.0%}-{s.max_leader_price:.0%}")
        print(f"Min ROI: {s.min_expected_roi:.0%}")
        print(f"Trade size: ${s.trade_size}")
        print("="*60 + "\n")

        # Load any unresolved trades
        for trade in self.store.get_unresolved_trades():
            self.active_trades[trade.market_id] = trade
            self.traded_markets.add(trade.market_id)

        await asyncio.gather(
            self.orderbook.stream(),
            self._refresh_markets(),
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
