#!/usr/bin/env python3
"""
Gabagool Paper Trading Engine v4.

Features:
- Predictive market discovery - polls at exact 15-min boundaries
- Streams prices via WebSocket
- Simulates fills at mid-price (no slippage for paper)
- Tracks positions in SQLite for persistence
- Logs all trades to CSV
- Abstracts execution for easy switch to live trading
- v4: Opportunistic dip-buying with FIFO matching
"""
import asyncio
import csv
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable
from pathlib import Path

from helpers import get_15m_markets, OrderbookStreamer, Market
from strategies import GabagoolStrategy, GabagoolAction


def get_next_15m_boundary() -> datetime:
    """Calculate the next 15-minute boundary (00, 15, 30, 45 minutes)."""
    now = datetime.now(timezone.utc)
    current_minute = now.minute
    next_boundary_minute = ((current_minute // 15) + 1) * 15

    if next_boundary_minute >= 60:
        next_boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        next_boundary = now.replace(minute=next_boundary_minute, second=0, microsecond=0)

    return next_boundary


def seconds_until_next_boundary() -> float:
    """Get seconds until next 15-min boundary."""
    now = datetime.now(timezone.utc)
    next_boundary = get_next_15m_boundary()
    return (next_boundary - now).total_seconds()


@dataclass
class EngineConfig:
    """Engine configuration for v4 dip-buy strategy."""
    starting_capital: float = 1000.0

    # v4 Dip-buy parameters
    yes_buy_threshold: float = 0.48
    no_buy_threshold: float = 0.48
    use_moving_average: bool = True
    ma_window_seconds: float = 30.0
    dip_below_ma_pct: float = 0.03

    # Position sizing
    trade_size: float = 10.0
    max_unmatched_cost: float = 50.0
    max_position_cost: float = 200.0

    # Timing
    cooldown_seconds: float = 5.0
    close_before_expiry_mins: float = 2.0

    assets: List[str] = None
    db_path: str = "data/gabagool.db"
    csv_path: str = "logs/trades.csv"
    tick_interval: float = 0.5

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
        side: str,
        shares: float,
        price: float,
    ) -> dict:
        """Execute a buy order."""
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

    async def execute_sell(
        self,
        market_id: str,
        side: str,
        shares: float,
        price: float,
    ) -> dict:
        """Execute a sell order."""
        return {
            'status': 'filled',
            'market_id': market_id,
            'side': side,
            'shares': shares,
            'price': price,
            'proceeds': shares * price,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'mode': 'paper',
        }


class LiveExecutionBackend(ExecutionBackend):
    """
    Live execution backend for real Polymarket trading.

    Requires:
    - POLYMARKET_PRIVATE_KEY environment variable
    - Funded wallet on Polygon with USDC

    Usage:
        from helpers.wallet import Wallet
        from helpers.clob_trading import CLOBClient

        wallet = Wallet.from_env()
        clob = CLOBClient(wallet)
        backend = LiveExecutionBackend(config, wallet, clob)
    """

    def __init__(
        self,
        config: EngineConfig,
        wallet: "Wallet" = None,
        clob_client: "CLOBClient" = None,
        risk_manager: "RiskManager" = None,
        dry_run: bool = False,
    ):
        super().__init__(config)
        self.wallet = wallet
        self.clob = clob_client
        self.risk_manager = risk_manager
        self.dry_run = dry_run
        self._order_map: Dict[str, str] = {}  # market_id+side -> order_id

    async def execute_buy(
        self,
        market_id: str,
        side: str,
        shares: float,
        price: float,
        token_id: str = None,
    ) -> dict:
        """
        Execute a live buy order on Polymarket CLOB.

        Args:
            market_id: Market condition ID
            side: "YES" or "NO"
            shares: Number of shares to buy
            price: Price per share
            token_id: Token ID for the outcome (required for live)

        Returns:
            Execution result dict
        """
        if self.clob is None:
            raise RuntimeError("CLOB client not initialized for live trading")

        # Risk checks
        amount = shares * price
        if self.risk_manager:
            # Get current balance from wallet
            current_balance = 1000.0  # Default fallback
            if self.wallet:
                try:
                    current_balance = float(self.wallet.get_usdc_balance())
                except Exception:
                    pass  # Use fallback

            can_trade, reason = self.risk_manager.can_trade(
                market_id=market_id,
                side=side,
                amount=amount,
                current_balance=current_balance,
            )
            if not can_trade:
                return {
                    'status': 'rejected',
                    'reason': reason,
                    'market_id': market_id,
                    'side': side,
                    'mode': 'live',
                }

        try:
            # Place order on CLOB
            result = await self.clob.place_order(
                token_id=token_id or market_id,
                side="BUY",
                size=shares,
                price=price,
            )

            order_id = result.get("id", "")
            self._order_map[f"{market_id}_{side}"] = order_id

            # Update risk manager
            if self.risk_manager:
                self.risk_manager.record_trade(
                    market_id=market_id,
                    side=side,
                    amount=shares * price,
                    shares=shares,
                    price=price,
                )

            return {
                'status': 'filled' if not self.dry_run else 'dry_run',
                'order_id': order_id,
                'market_id': market_id,
                'side': side,
                'shares': shares,
                'price': price,
                'cost': shares * price,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'mode': 'live' if not self.dry_run else 'dry_run',
            }

        except Exception as e:
            return {
                'status': 'error',
                'error': str(e),
                'market_id': market_id,
                'side': side,
                'mode': 'live',
            }

    async def execute_sell(
        self,
        market_id: str,
        side: str,
        shares: float,
        price: float,
        token_id: str = None,
    ) -> dict:
        """
        Execute a live sell order on Polymarket CLOB.

        Args:
            market_id: Market condition ID
            side: "YES" or "NO"
            shares: Number of shares to sell
            price: Price per share
            token_id: Token ID for the outcome

        Returns:
            Execution result dict
        """
        if self.clob is None:
            raise RuntimeError("CLOB client not initialized for live trading")

        try:
            # Place sell order
            result = await self.clob.place_order(
                token_id=token_id or market_id,
                side="SELL",
                size=shares,
                price=price,
            )

            order_id = result.get("id", "")

            # Record the sell in risk manager (negative amount for sells)
            if self.risk_manager:
                self.risk_manager.record_trade(
                    market_id=market_id,
                    side=side,
                    amount=-(shares * price),  # Negative for sell/close
                    shares=-shares,
                    price=price,
                )

            return {
                'status': 'filled' if not self.dry_run else 'dry_run',
                'order_id': order_id,
                'market_id': market_id,
                'side': side,
                'shares': shares,
                'price': price,
                'proceeds': shares * price,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'mode': 'live' if not self.dry_run else 'dry_run',
            }

        except Exception as e:
            return {
                'status': 'error',
                'error': str(e),
                'market_id': market_id,
                'side': side,
                'mode': 'live',
            }

    async def cancel_order(self, market_id: str, side: str) -> dict:
        """Cancel an open order."""
        order_key = f"{market_id}_{side}"
        order_id = self._order_map.get(order_key)

        if not order_id:
            return {'status': 'not_found', 'market_id': market_id, 'side': side}

        try:
            await self.clob.cancel_order(order_id)
            del self._order_map[order_key]
            return {'status': 'cancelled', 'order_id': order_id}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}


class PersistenceManager:
    """SQLite persistence for positions and trades."""

    def __init__(self, db_path: str, csv_path: str):
        self.db_path = db_path
        self.csv_path = csv_path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(csv_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()
        self._init_csv()

    def _init_db(self):
        """Initialize SQLite database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Trades table (v4 format)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                cost REAL,
                proceeds REAL,
                timestamp TEXT NOT NULL
            )
        """)

        # Positions table (v4 format)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions_v4 (
                market_id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                unmatched_yes_shares REAL DEFAULT 0,
                unmatched_no_shares REAL DEFAULT 0,
                unmatched_yes_cost REAL DEFAULT 0,
                unmatched_no_cost REAL DEFAULT 0,
                matched_shares REAL DEFAULT 0,
                matched_cost_yes REAL DEFAULT 0,
                matched_cost_no REAL DEFAULT 0,
                locked_profit REAL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # Outcomes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                market_id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                matched_shares REAL,
                locked_profit REAL,
                unmatched_yes REAL,
                unmatched_no REAL,
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
                    'timestamp', 'market_id', 'asset', 'side', 'action',
                    'shares', 'price', 'cost', 'proceeds', 'locked_profit'
                ])

    def save_trade(self, market_id: str, asset: str, trade_info: dict):
        """Save a trade to DB and CSV."""
        now = datetime.now(timezone.utc).isoformat()
        action = trade_info.get('action', 'buy')
        side = trade_info['side']
        shares = trade_info['shares']
        price = trade_info['price']
        cost = trade_info.get('cost')
        proceeds = trade_info.get('proceeds')
        locked_profit = trade_info.get('locked_profit', 0)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO trades (market_id, asset, side, action, shares, price, cost, proceeds, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (market_id, asset, side, action, shares, price, cost, proceeds, now))

        conn.commit()
        conn.close()

        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                now, market_id, asset, side, action,
                shares, price, cost, proceeds, locked_profit
            ])

    def save_position(self, market_id: str, asset: str, position: dict):
        """Save position state to DB (v4 format)."""
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO positions_v4
            (market_id, asset, unmatched_yes_shares, unmatched_no_shares,
             unmatched_yes_cost, unmatched_no_cost, matched_shares,
             matched_cost_yes, matched_cost_no, locked_profit,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM positions_v4 WHERE market_id = ?), ?), ?)
        """, (
            market_id,
            asset,
            position.get('unmatched_yes_shares', 0),
            position.get('unmatched_no_shares', 0),
            position.get('unmatched_yes_cost', 0),
            position.get('unmatched_no_cost', 0),
            position.get('matched_shares', 0),
            position.get('matched_cost_yes', 0),
            position.get('matched_cost_no', 0),
            position.get('locked_profit', 0),
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
            (market_id, asset, matched_shares, locked_profit,
             unmatched_yes, unmatched_no, final_pnl, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_id,
            summary['asset'],
            summary.get('matched_shares', 0),
            summary.get('locked_profit', 0),
            summary.get('unmatched_yes_shares', 0),
            summary.get('unmatched_no_shares', 0),
            summary.get('final_pnl', 0),
            now,
        ))

        cursor.execute("DELETE FROM positions_v4 WHERE market_id = ?", (market_id,))

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
            }
        return {}


class GabagoolEngine:
    """Main paper trading engine v4."""

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

        # Strategy v4: Opportunistic Dip-Buying
        self.strategy = GabagoolStrategy(
            yes_buy_threshold=self.config.yes_buy_threshold,
            no_buy_threshold=self.config.no_buy_threshold,
            use_moving_average=self.config.use_moving_average,
            ma_window_seconds=self.config.ma_window_seconds,
            dip_below_ma_pct=self.config.dip_below_ma_pct,
            trade_size=self.config.trade_size,
            max_unmatched_cost=self.config.max_unmatched_cost,
            max_position_cost=self.config.max_position_cost,
            cooldown_seconds=self.config.cooldown_seconds,
            close_before_expiry_mins=self.config.close_before_expiry_mins,
        )

        self.persistence = PersistenceManager(
            self.config.db_path,
            self.config.csv_path,
        )

        self.orderbook_streamer = OrderbookStreamer()

        self.markets: Dict[str, Market] = {}
        self.running = False
        self.capital = self.config.starting_capital
        self.total_pnl = 0.0
        self.trade_count = 0
        self.yes_buys = 0
        self.no_buys = 0
        self.markets_completed = 0

        saved_stats = self.persistence.load_stats()
        if saved_stats:
            self.capital = saved_stats.get('current_capital', self.config.starting_capital)
            self.total_pnl = saved_stats.get('total_pnl', 0)
            self.trade_count = saved_stats.get('total_trades', 0)
            self.yes_buys = saved_stats.get('total_yes_buys', 0)
            self.no_buys = saved_stats.get('total_no_buys', 0)
            self.markets_completed = saved_stats.get('markets_completed', 0)

    def refresh_markets(self):
        """Find active 15-min markets."""
        print("\n" + "=" * 60)
        print("GABAGOOL v4 DIP-BUY")
        print("=" * 60)

        markets = get_15m_markets(assets=self.config.assets)
        now = datetime.now(timezone.utc)

        new_market_ids = set()

        for m in markets:
            mins_left = (m.end_time - now).total_seconds() / 60
            if mins_left < 1.0:
                continue

            new_market_ids.add(m.condition_id)

            if m.condition_id not in self.markets:
                print(f"\n{m.asset} 15m | {mins_left:.1f}m left")
                print(f"  YES: {m.price_up:.3f} | NO: {m.price_down:.3f}")
                self.orderbook_streamer.subscribe(m.condition_id, m.token_up, m.token_down)

            self.markets[m.condition_id] = m

        expired = [cid for cid in self.markets if cid not in new_market_ids]
        for cid in expired:
            self._handle_expired_market(cid)

        if not self.markets:
            print("\nNo active markets!")
        else:
            self.orderbook_streamer.clear_stale(set(self.markets.keys()))

    def _handle_expired_market(self, market_id: str):
        """Handle an expired market."""
        if market_id not in self.markets:
            return

        market = self.markets[market_id]
        print(f"\n  EXPIRED: {market.asset}")

        summary = self.strategy.clear_expired_market(market_id)
        if summary:
            final_pnl = summary.get('final_pnl', 0)
            self.total_pnl += final_pnl
            self.capital += final_pnl
            self.markets_completed += 1

            print(f"    Locked Profit: ${summary['locked_profit']:.2f}")
            print(f"    Unmatched YES: {summary.get('unmatched_yes_shares', 0):.1f}")
            print(f"    Unmatched NO: {summary.get('unmatched_no_shares', 0):.1f}")

            self.persistence.save_outcome(market_id, summary)

        del self.markets[market_id]

    async def decision_loop(self):
        """Main trading decision loop."""
        tick = 0
        last_boundary_check = None
        aggressive_poll_until = None

        while self.running:
            await asyncio.sleep(self.config.tick_interval)
            tick += 1
            now = datetime.now(timezone.utc)

            # Predictive scheduling
            current_boundary = now.replace(second=0, microsecond=0)
            current_boundary = current_boundary.replace(minute=(now.minute // 15) * 15)

            seconds_into_window = (now - current_boundary).total_seconds()
            should_refresh = False

            if last_boundary_check != current_boundary:
                if seconds_into_window >= 2:
                    last_boundary_check = current_boundary
                    aggressive_poll_until = current_boundary + timedelta(seconds=60)
                    print(f"\n[{now.strftime('%H:%M:%S')}] New 15-min window!")
                    should_refresh = True

            if aggressive_poll_until and now < aggressive_poll_until:
                if tick % 10 == 0:
                    market_count = len(self.markets)
                    self.refresh_markets()
                    new_count = len(self.markets)
                    if new_count > market_count:
                        print(f"  Found {new_count - market_count} new market(s)!")
                        aggressive_poll_until = None
            elif aggressive_poll_until and now >= aggressive_poll_until:
                aggressive_poll_until = None

            if should_refresh:
                self.refresh_markets()

            expired = [cid for cid, m in self.markets.items() if m.end_time <= now]
            for cid in expired:
                self._handle_expired_market(cid)

            if not self.markets:
                continue

            # Process each market
            for cid, market in list(self.markets.items()):
                mins_left = (market.end_time - now).total_seconds() / 60
                if mins_left < 0.5:
                    continue

                ob_up = self.orderbook_streamer.get_orderbook(cid, "UP")
                ob_down = self.orderbook_streamer.get_orderbook(cid, "DOWN")

                if not ob_up or not ob_down:
                    continue

                yes_price = ob_up.mid_price or market.price_up
                no_price = ob_down.mid_price or market.price_down

                if yes_price is None or no_price is None:
                    continue

                time_left = market.end_time - now

                # Run v4 strategy
                action, trade_info = self.strategy.on_price_update(
                    market_id=cid,
                    asset=market.asset,
                    yes_price=yes_price,
                    no_price=no_price,
                    time_left=time_left,
                )

                # Execute based on action
                if action == GabagoolAction.BUY_YES and trade_info:
                    await self._execute_buy(cid, market.asset, 'YES', trade_info)
                elif action == GabagoolAction.BUY_NO and trade_info:
                    await self._execute_buy(cid, market.asset, 'NO', trade_info)
                elif action == GabagoolAction.SELL_YES and trade_info:
                    await self._execute_sell(cid, market.asset, 'YES', trade_info)
                elif action == GabagoolAction.SELL_NO and trade_info:
                    await self._execute_sell(cid, market.asset, 'NO', trade_info)

            if tick % 20 == 0:
                self._print_status()

            if self.on_state_update:
                self.on_state_update(self._get_state())

    async def _execute_buy(self, market_id: str, asset: str, side: str, trade_info: dict):
        """Execute a single-side buy."""
        shares = trade_info['shares']
        price = trade_info['price']

        result = await self.execution.execute_buy(
            market_id=market_id,
            side=side,
            shares=shares,
            price=price,
        )

        if result['status'] == 'filled':
            self.trade_count += 1
            if side == 'YES':
                self.yes_buys += 1
            else:
                self.no_buys += 1

            trade_info['action'] = 'buy'
            self.persistence.save_trade(market_id, asset, trade_info)

            position = self.strategy.get_position_summary(market_id)
            if position:
                self.persistence.save_position(market_id, asset, position)

            unmatched_yes = trade_info.get('unmatched_yes', 0)
            unmatched_no = trade_info.get('unmatched_no', 0)
            print(f"    BUY {side} {asset} | {shares:.1f} @ {price:.3f} | "
                  f"unmatched: {unmatched_yes:.1f}Y/{unmatched_no:.1f}N")

            if self.on_trade:
                self.on_trade({'market_id': market_id, 'asset': asset, **trade_info})

    async def _execute_sell(self, market_id: str, asset: str, side: str, trade_info: dict):
        """Execute a single-side sell (close unmatched before expiry)."""
        shares = trade_info['shares']
        price = trade_info['price']

        # Update strategy position first
        position = self.strategy.positions.get(market_id)
        if position:
            if side == 'YES':
                position.remove_yes_shares(shares, price)
            else:
                position.remove_no_shares(shares, price)

        result = await self.execution.execute_sell(
            market_id=market_id,
            side=side,
            shares=shares,
            price=price,
        )

        if result['status'] == 'filled':
            self.trade_count += 1

            trade_info['action'] = 'sell'
            self.persistence.save_trade(market_id, asset, trade_info)

            position_summary = self.strategy.get_position_summary(market_id)
            if position_summary:
                self.persistence.save_position(market_id, asset, position_summary)

            proceeds = trade_info.get('proceeds', shares * price)
            reason = trade_info.get('reason', '')
            print(f"    SELL {side} {asset} | {shares:.1f} @ {price:.3f} | "
                  f"proceeds=${proceeds:.2f} | {reason}")

            if self.on_trade:
                self.on_trade({'market_id': market_id, 'asset': asset, **trade_info})

    def _print_status(self):
        """Print current status."""
        now = datetime.now(timezone.utc)

        print(f"\n[{now.strftime('%H:%M:%S')}] GABAGOOL v4")
        print(f"  Capital: ${self.capital:.2f} | PnL: ${self.total_pnl:+.2f} | "
              f"Trades: {self.trade_count} ({self.yes_buys}Y/{self.no_buys}N)")

        total_locked = self.strategy.get_total_locked_profit()
        total_risk = self.strategy.get_total_unrealized_risk()
        total_cost = self.strategy.get_total_cost()
        print(f"  Locked: ${total_locked:.2f} | At Risk: ${total_risk:.2f} | Invested: ${total_cost:.2f}")

        for cid, market in self.markets.items():
            mins_left = (market.end_time - now).total_seconds() / 60
            summary = self.strategy.get_position_summary(cid)

            ob_up = self.orderbook_streamer.get_orderbook(cid, "UP")
            ob_down = self.orderbook_streamer.get_orderbook(cid, "DOWN")
            yes_price = (ob_up.mid_price if ob_up and ob_up.mid_price else None) or market.price_up or 0.5
            no_price = (ob_down.mid_price if ob_down and ob_down.mid_price else None) or market.price_down or 0.5

            if summary:
                print(f"  {market.asset}: matched={summary['matched_shares']:.1f} "
                      f"unmatched={summary['unmatched_yes_shares']:.1f}Y/{summary['unmatched_no_shares']:.1f}N "
                      f"| locked=${summary['locked_profit']:.2f} | {mins_left:.1f}m")
            else:
                print(f"  {market.asset}: YES={yes_price:.2f} NO={no_price:.2f} | {mins_left:.1f}m")

        self.persistence.update_stats({
            'starting_capital': self.config.starting_capital,
            'current_capital': self.capital,
            'total_pnl': self.total_pnl,
            'total_trades': self.trade_count,
            'total_yes_buys': self.yes_buys,
            'total_no_buys': self.no_buys,
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

            yes_price = (ob_up.mid_price if ob_up and ob_up.mid_price else None) or market.price_up or 0.5
            no_price = (ob_down.mid_price if ob_down and ob_down.mid_price else None) or market.price_down or 0.5

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
            'yes_buys': self.yes_buys,
            'no_buys': self.no_buys,
            'markets_completed': self.markets_completed,
            'total_locked_profit': self.strategy.get_total_locked_profit(),
            'total_unrealized_risk': self.strategy.get_total_unrealized_risk(),
            'total_invested': self.strategy.get_total_cost(),
            'markets': markets_state,
            'positions': positions_state,
        }

    async def run(self):
        """Run the trading engine."""
        self.running = True

        next_boundary = get_next_15m_boundary()
        secs_until = seconds_until_next_boundary()
        print(f"\n  Next 15-min window: {next_boundary.strftime('%H:%M:%S')} UTC ({secs_until:.0f}s)")

        self.refresh_markets()

        if not self.markets:
            print("No active markets - waiting for next 15-min window...")
            wait_time = min(secs_until + 3, 30)
            print(f"  Checking again in {wait_time:.0f}s...")
            await asyncio.sleep(wait_time)
            self.refresh_markets()

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
        print("FINAL RESULTS - v4 DIP-BUY STRATEGY")
        print("=" * 60)
        print(f"Starting Capital: ${self.config.starting_capital:.2f}")
        print(f"Final Capital: ${self.capital:.2f}")
        print(f"Total P&L: ${self.total_pnl:+.2f}")
        print(f"Total Trades: {self.trade_count} ({self.yes_buys} YES / {self.no_buys} NO)")
        print(f"Markets Completed: {self.markets_completed}")
        print(f"Total Locked Profit: ${self.strategy.get_total_locked_profit():.2f}")
        print(f"Unrealized Risk: ${self.strategy.get_total_unrealized_risk():.2f}")

    def stop(self):
        """Stop the engine."""
        self.running = False


if __name__ == "__main__":
    config = EngineConfig(
        starting_capital=1000.0,
        yes_buy_threshold=0.48,
        no_buy_threshold=0.48,
        trade_size=10.0,
        assets=["BTC", "ETH"],
    )

    engine = GabagoolEngine(config)
    asyncio.run(engine.run())
