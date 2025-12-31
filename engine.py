#!/usr/bin/env python3
"""
Gabagool Paper Trading Engine.

Features:
- Polls for new 15-min markets every minute
- Streams prices via WebSocket
- Simulates fills at mid-price (no slippage for paper)
- Tracks positions in SQLite for persistence
- Logs all trades to CSV
- Abstracts execution for easy switch to live trading
"""
import asyncio
import csv
import sqlite3
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable
from pathlib import Path

from helpers import get_15m_markets, OrderbookStreamer, Market
from strategies import GabagoolStrategy, GabagoolAction


@dataclass
class EngineConfig:
    """Engine configuration."""
    starting_capital: float = 1000.0
    trade_size: float = 10.0
    dip_threshold: float = 0.05
    lookback_periods: int = 60  # v2: increased from 20
    min_periods: int = 30  # v2: increased from 10
    max_position_per_side: float = 50.0  # v2: reduced from 100
    trade_cooldown_seconds: float = 30.0  # v2: new
    min_time_left_minutes: float = 3.0  # v2: new
    imbalance_threshold: float = 1.5  # v2: new
    assets: List[str] = None  # Default: BTC, ETH
    db_path: str = "data/gabagool.db"
    csv_path: str = "logs/trades.csv"
    tick_interval: float = 0.5  # Seconds between price checks
    market_refresh_interval: float = 60.0  # Seconds between market discovery

    def __post_init__(self):
        if self.assets is None:
            self.assets = ["BTC", "ETH"]


class ExecutionBackend:
    """
    Abstract execution backend.
    Override for live trading.
    """

    def __init__(self, config: EngineConfig):
        self.config = config

    async def execute_buy(
        self,
        market_id: str,
        side: str,  # "YES" or "NO"
        shares: float,
        price: float,
    ) -> dict:
        """
        Execute a buy order.
        Returns trade confirmation dict.
        """
        # Paper trading: instant fill at requested price
        return {
            'status': 'filled',
            'market_id': market_id,
            'side': side,
            'shares': shares,
            'price': price,
            'cost': shares * price,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'mode': 'paper',
        }


class LiveExecutionBackend(ExecutionBackend):
    """
    Live trading backend (placeholder).
    TODO: Implement with Polymarket CLOB API.
    """

    def __init__(self, config: EngineConfig, api_key: str, api_secret: str):
        super().__init__(config)
        self.api_key = api_key
        self.api_secret = api_secret
        # TODO: Initialize Polymarket client

    async def execute_buy(
        self,
        market_id: str,
        side: str,
        shares: float,
        price: float,
    ) -> dict:
        # TODO: Implement live execution
        raise NotImplementedError("Live trading not yet implemented")


class PersistenceManager:
    """SQLite persistence for positions and trades."""

    def __init__(self, db_path: str, csv_path: str):
        self.db_path = db_path
        self.csv_path = csv_path

        # Ensure directories exist
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(csv_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()
        self._init_csv()

    def _init_db(self):
        """Initialize SQLite database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Positions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                market_id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                shares_yes REAL DEFAULT 0,
                shares_no REAL DEFAULT 0,
                cost_yes REAL DEFAULT 0,
                cost_no REAL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                cost REAL NOT NULL,
                ma REAL,
                dip_pct REAL,
                timestamp TEXT NOT NULL
            )
        """)

        # Market outcomes table (for resolved markets)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                market_id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                outcome TEXT,
                shares_yes REAL,
                shares_no REAL,
                cost_yes REAL,
                cost_no REAL,
                locked_profit REAL,
                final_pnl REAL,
                resolved_at TEXT
            )
        """)

        # Stats table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                starting_capital REAL,
                current_capital REAL,
                total_pnl REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                total_yes_buys INTEGER DEFAULT 0,
                total_no_buys INTEGER DEFAULT 0,
                markets_completed INTEGER DEFAULT 0,
                started_at TEXT,
                updated_at TEXT
            )
        """)

        conn.commit()
        conn.close()

    def _init_csv(self):
        """Initialize CSV trade log."""
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_id', 'asset', 'side', 'shares',
                    'price', 'cost', 'ma', 'dip_pct', 'locked_profit'
                ])

    def save_trade(self, market_id: str, asset: str, trade_info: dict, locked_profit: float):
        """Save a trade to DB and CSV."""
        now = datetime.now(timezone.utc).isoformat()

        # SQLite
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO trades (market_id, asset, side, shares, price, cost, ma, dip_pct, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_id,
            asset,
            trade_info['side'],
            trade_info['shares'],
            trade_info['price'],
            trade_info['cost'],
            trade_info.get('ma'),
            trade_info.get('dip_pct'),
            now,
        ))

        conn.commit()
        conn.close()

        # CSV
        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                now,
                market_id,
                asset,
                trade_info['side'],
                trade_info['shares'],
                trade_info['price'],
                trade_info['cost'],
                trade_info.get('ma'),
                trade_info.get('dip_pct'),
                locked_profit,
            ])

    def save_position(self, market_id: str, asset: str, position: dict):
        """Save position state to DB."""
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (market_id, asset, shares_yes, shares_no, cost_yes, cost_no, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM positions WHERE market_id = ?), ?), ?)
        """, (
            market_id,
            asset,
            position['shares_yes'],
            position['shares_no'],
            position['cost_yes'],
            position['cost_no'],
            market_id,
            now,
            now,
        ))

        conn.commit()
        conn.close()

    def save_outcome(self, market_id: str, summary: dict):
        """Save market outcome when resolved."""
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO outcomes
            (market_id, asset, shares_yes, shares_no, cost_yes, cost_no, locked_profit, final_pnl, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_id,
            summary['asset'],
            summary['shares_yes'],
            summary['shares_no'],
            summary['cost_yes'],
            summary['cost_no'],
            summary['locked_profit'],
            summary.get('final_pnl', 0),
            now,
        ))

        # Remove from active positions
        cursor.execute("DELETE FROM positions WHERE market_id = ?", (market_id,))

        conn.commit()
        conn.close()

    def update_stats(self, stats: dict):
        """Update overall stats."""
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO stats
            (id, starting_capital, current_capital, total_pnl, total_trades,
             total_yes_buys, total_no_buys, markets_completed, started_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT started_at FROM stats WHERE id = 1), ?), ?)
        """, (
            stats.get('starting_capital', 1000),
            stats.get('current_capital', 1000),
            stats.get('total_pnl', 0),
            stats.get('total_trades', 0),
            stats.get('total_yes_buys', 0),
            stats.get('total_no_buys', 0),
            stats.get('markets_completed', 0),
            now,
            now,
        ))

        conn.commit()
        conn.close()

    def load_stats(self) -> dict:
        """Load stats from DB."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM stats WHERE id = 1")
        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                'starting_capital': row[1],
                'current_capital': row[2],
                'total_pnl': row[3],
                'total_trades': row[4],
                'total_yes_buys': row[5],
                'total_no_buys': row[6],
                'markets_completed': row[7],
                'started_at': row[8],
                'updated_at': row[9],
            }
        return {}

    def load_active_positions(self) -> Dict[str, dict]:
        """Load active positions from DB."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM positions")
        rows = cursor.fetchall()
        conn.close()

        positions = {}
        for row in rows:
            positions[row[0]] = {
                'market_id': row[0],
                'asset': row[1],
                'shares_yes': row[2],
                'shares_no': row[3],
                'cost_yes': row[4],
                'cost_no': row[5],
            }
        return positions


class GabagoolEngine:
    """
    Main paper trading engine.
    """

    def __init__(
        self,
        config: EngineConfig = None,
        execution_backend: ExecutionBackend = None,
        on_trade: Callable = None,
        on_state_update: Callable = None,
    ):
        self.config = config or EngineConfig()
        self.execution = execution_backend or ExecutionBackend(self.config)
        self.on_trade = on_trade
        self.on_state_update = on_state_update

        # Strategy (v2: with new parameters)
        self.strategy = GabagoolStrategy(
            dip_threshold=self.config.dip_threshold,
            lookback_periods=self.config.lookback_periods,
            min_periods=self.config.min_periods,
            max_position_per_side=self.config.max_position_per_side,
            trade_cooldown_seconds=self.config.trade_cooldown_seconds,
            min_time_left_minutes=self.config.min_time_left_minutes,
            imbalance_threshold=self.config.imbalance_threshold,
        )

        # Persistence
        self.persistence = PersistenceManager(
            self.config.db_path,
            self.config.csv_path,
        )

        # WebSocket streamer
        self.orderbook_streamer = OrderbookStreamer()

        # State
        self.markets: Dict[str, Market] = {}
        self.running = False
        self.capital = self.config.starting_capital
        self.total_pnl = 0.0
        self.trade_count = 0
        self.markets_completed = 0

        # Load saved stats
        saved_stats = self.persistence.load_stats()
        if saved_stats:
            self.capital = saved_stats.get('current_capital', self.config.starting_capital)
            self.total_pnl = saved_stats.get('total_pnl', 0)
            self.trade_count = saved_stats.get('total_trades', 0)
            self.markets_completed = saved_stats.get('markets_completed', 0)

    def refresh_markets(self):
        """Find active 15-min markets."""
        print("\n" + "=" * 60)
        print("GABAGOOL PAPER TRADING")
        print("=" * 60)

        markets = get_15m_markets(assets=self.config.assets)
        now = datetime.now(timezone.utc)

        # Track current market IDs
        new_market_ids = set()

        for m in markets:
            mins_left = (m.end_time - now).total_seconds() / 60
            if mins_left < 1.0:  # Skip markets about to expire
                continue

            new_market_ids.add(m.condition_id)

            if m.condition_id not in self.markets:
                print(f"\n{m.asset} 15m | {mins_left:.1f}m left")
                print(f"  UP: {m.price_up:.3f} | DOWN: {m.price_down:.3f}")
                self.orderbook_streamer.subscribe(m.condition_id, m.token_up, m.token_down)

            self.markets[m.condition_id] = m

        # Handle expired markets
        expired = [cid for cid in self.markets if cid not in new_market_ids]
        for cid in expired:
            self._handle_expired_market(cid)

        if not self.markets:
            print("\nNo active markets!")
        else:
            # Clear stale orderbook subscriptions
            self.orderbook_streamer.clear_stale(set(self.markets.keys()))

    def _handle_expired_market(self, market_id: str):
        """Handle an expired market - calculate final P&L."""
        if market_id not in self.markets:
            return

        market = self.markets[market_id]
        print(f"\n  EXPIRED: {market.asset}")

        # Get final position summary
        summary = self.strategy.clear_expired_market(market_id)
        if summary:
            final_pnl = summary.get('final_pnl', 0)
            self.total_pnl += final_pnl
            self.capital += final_pnl
            self.markets_completed += 1

            print(f"    Final P&L: ${final_pnl:+.2f}")
            print(f"    Locked Profit: ${summary['locked_profit']:.2f}")

            # Persist outcome
            self.persistence.save_outcome(market_id, summary)

        del self.markets[market_id]

    async def decision_loop(self):
        """Main trading decision loop."""
        tick = 0
        last_refresh = 0

        while self.running:
            await asyncio.sleep(self.config.tick_interval)
            tick += 1
            now = datetime.now(timezone.utc)

            # Refresh markets periodically
            elapsed = tick * self.config.tick_interval
            if elapsed - last_refresh >= self.config.market_refresh_interval:
                self.refresh_markets()
                last_refresh = elapsed

            # Check expired markets
            expired = [cid for cid, m in self.markets.items() if m.end_time <= now]
            for cid in expired:
                self._handle_expired_market(cid)

            if not self.markets:
                continue

            # Process each market
            for cid, market in list(self.markets.items()):
                # Skip if near expiry
                mins_left = (market.end_time - now).total_seconds() / 60
                if mins_left < 0.5:
                    continue

                # Get orderbook prices
                ob_up = self.orderbook_streamer.get_orderbook(cid, "UP")
                ob_down = self.orderbook_streamer.get_orderbook(cid, "DOWN")

                if not ob_up or not ob_down:
                    continue

                yes_price = ob_up.mid_price or market.price_up
                no_price = ob_down.mid_price or market.price_down

                # v2: Calculate time left for expiry awareness
                time_left = market.end_time - now

                # Run strategy (v2: pass time_left)
                action, trade_info = self.strategy.on_price_update(
                    market_id=cid,
                    asset=market.asset,
                    yes_price=yes_price,
                    no_price=no_price,
                    trade_size=self.config.trade_size,
                    time_left=time_left,
                )

                # Execute if action taken
                if action != GabagoolAction.HOLD and trade_info:
                    await self._execute_trade(cid, market.asset, trade_info)

            # Status update every 20 ticks
            if tick % 20 == 0:
                self._print_status()

            # Emit state update
            if self.on_state_update:
                self.on_state_update(self._get_state())

    async def _execute_trade(self, market_id: str, asset: str, trade_info: dict):
        """Execute a trade and persist."""
        # Paper execution
        result = await self.execution.execute_buy(
            market_id=market_id,
            side=trade_info['side'],
            shares=trade_info['shares'],
            price=trade_info['price'],
        )

        if result['status'] == 'filled':
            self.trade_count += 1

            # Get current locked profit
            locked_profit = self.strategy.calculate_locked_profit(market_id)

            # Persist
            self.persistence.save_trade(market_id, asset, trade_info, locked_profit)

            position = self.strategy.get_position_summary(market_id)
            if position:
                self.persistence.save_position(market_id, asset, position)

            # Log
            print(f"    BUY {trade_info['side']} {asset} @ {trade_info['price']:.3f} "
                  f"| shares={trade_info['shares']:.2f} | cost=${trade_info['cost']:.2f} "
                  f"| locked=${locked_profit:.2f}")

            # Callback
            if self.on_trade:
                self.on_trade({
                    'market_id': market_id,
                    'asset': asset,
                    **trade_info,
                    'locked_profit': locked_profit,
                })

    def _print_status(self):
        """Print current status."""
        now = datetime.now(timezone.utc)

        print(f"\n[{now.strftime('%H:%M:%S')}] GABAGOOL")
        print(f"  Capital: ${self.capital:.2f} | PnL: ${self.total_pnl:+.2f} | Trades: {self.trade_count}")

        total_locked = self.strategy.get_total_locked_profit()
        total_exposure = self.strategy.get_total_exposure()
        print(f"  Locked Profit: ${total_locked:.2f} | Exposure: ${total_exposure:.2f}")

        for cid, market in self.markets.items():
            mins_left = (market.end_time - now).total_seconds() / 60
            summary = self.strategy.get_position_summary(cid)

            if summary:
                print(f"  {market.asset}: YES={summary['shares_yes']:.1f} NO={summary['shares_no']:.1f} "
                      f"| locked=${summary['locked_profit']:.2f} | {mins_left:.1f}m")
            else:
                print(f"  {market.asset}: (no position) | {mins_left:.1f}m")

        # Persist stats
        self.persistence.update_stats({
            'starting_capital': self.config.starting_capital,
            'current_capital': self.capital,
            'total_pnl': self.total_pnl,
            'total_trades': self.trade_count,
            'total_yes_buys': self.strategy.total_yes_buys,
            'total_no_buys': self.strategy.total_no_buys,
            'markets_completed': self.markets_completed,
        })

    def _get_state(self) -> dict:
        """Get current state for dashboard."""
        now = datetime.now(timezone.utc)

        markets_state = {}
        positions_state = {}

        for cid, market in self.markets.items():
            mins_left = (market.end_time - now).total_seconds() / 60

            ob_up = self.orderbook_streamer.get_orderbook(cid, "UP")
            ob_down = self.orderbook_streamer.get_orderbook(cid, "DOWN")

            yes_price = ob_up.mid_price if ob_up else market.price_up
            no_price = ob_down.mid_price if ob_down else market.price_down

            markets_state[cid] = {
                'asset': market.asset,
                'yes_price': yes_price,
                'no_price': no_price,
                'time_left': mins_left,
            }

            summary = self.strategy.get_position_summary(cid)
            if summary:
                positions_state[cid] = summary

        return {
            'capital': self.capital,
            'total_pnl': self.total_pnl,
            'trade_count': self.trade_count,
            'markets_completed': self.markets_completed,
            'total_locked_profit': self.strategy.get_total_locked_profit(),
            'total_exposure': self.strategy.get_total_exposure(),
            'markets': markets_state,
            'positions': positions_state,
        }

    async def run(self):
        """Run the trading engine."""
        self.running = True
        self.refresh_markets()

        if not self.markets:
            print("No markets to trade!")
            print("Waiting for markets...")
            while self.running and not self.markets:
                await asyncio.sleep(30)
                self.refresh_markets()

        # Start orderbook streaming and decision loop
        tasks = [
            self.orderbook_streamer.stream(),
            self.decision_loop(),
        ]

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print("\n\nShutting down...")
            self.running = False
            self.orderbook_streamer.stop()
            self._print_final_stats()

    def _print_final_stats(self):
        """Print final results."""
        print("\n" + "=" * 60)
        print("FINAL RESULTS")
        print("=" * 60)
        print(f"Starting Capital: ${self.config.starting_capital:.2f}")
        print(f"Final Capital: ${self.capital:.2f}")
        print(f"Total P&L: ${self.total_pnl:+.2f}")
        print(f"Total Trades: {self.trade_count}")
        print(f"  YES buys: {self.strategy.total_yes_buys}")
        print(f"  NO buys: {self.strategy.total_no_buys}")
        print(f"Markets Completed: {self.markets_completed}")
        print(f"Total Locked Profit: ${self.strategy.get_total_locked_profit():.2f}")

    def stop(self):
        """Stop the engine."""
        self.running = False


if __name__ == "__main__":
    # Quick test
    config = EngineConfig(
        starting_capital=1000.0,
        trade_size=10.0,
        dip_threshold=0.05,
        assets=["BTC", "ETH"],
    )

    engine = GabagoolEngine(config)
    asyncio.run(engine.run())
