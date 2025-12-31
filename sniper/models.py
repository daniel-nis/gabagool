"""
Data models for Late-Game Sniper.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import sqlite3
import os


@dataclass
class SnipeTrade:
    """A single snipe trade."""
    market_id: str
    asset: str              # BTC, ETH
    side: str               # UP, DOWN
    entry_price: float      # Price paid (e.g., 0.85)
    entry_time: datetime
    shares: float           # Number of shares bought
    cost: float             # Total cost (entry_price * shares)

    # Resolution
    resolved: bool = False
    exit_price: Optional[float] = None  # 1.0 (win) or 0.0 (loss)
    pnl: Optional[float] = None
    roi: Optional[float] = None

    # Metadata
    id: Optional[int] = None
    time_remaining_at_entry: Optional[float] = None
    binance_confirmed: Optional[bool] = None
    skip_reason: Optional[str] = None  # If we skipped, why


@dataclass
class SnipeOpportunity:
    """A potential snipe opportunity (entered or skipped)."""
    market_id: str
    asset: str
    timestamp: datetime
    time_remaining: float

    # Market state
    up_price: float
    down_price: float
    leader: str             # UP or DOWN
    leader_price: float
    spread: float

    # Decision
    entered: bool
    skip_reason: Optional[str] = None

    # Binance confirmation
    binance_price: Optional[float] = None
    binance_direction: Optional[str] = None  # UP or DOWN based on price vs open
    binance_confirms: Optional[bool] = None


class TradeStore:
    """SQLite persistence for snipe trades."""

    def __init__(self, db_path: str = "sniper/data/trades.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database tables."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # Trades table
        c.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                shares REAL NOT NULL,
                cost REAL NOT NULL,
                resolved INTEGER DEFAULT 0,
                exit_price REAL,
                pnl REAL,
                roi REAL,
                time_remaining_at_entry REAL,
                binance_confirmed INTEGER
            )
        ''')

        # Opportunities table (for analysis)
        c.execute('''
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                time_remaining REAL NOT NULL,
                up_price REAL NOT NULL,
                down_price REAL NOT NULL,
                leader TEXT NOT NULL,
                leader_price REAL NOT NULL,
                spread REAL NOT NULL,
                entered INTEGER NOT NULL,
                skip_reason TEXT,
                binance_price REAL,
                binance_direction TEXT,
                binance_confirms INTEGER
            )
        ''')

        conn.commit()
        conn.close()

    def save_trade(self, trade: SnipeTrade) -> int:
        """Save a trade and return its ID."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('''
            INSERT INTO trades (
                market_id, asset, side, entry_price, entry_time,
                shares, cost, resolved, exit_price, pnl, roi,
                time_remaining_at_entry, binance_confirmed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.market_id, trade.asset, trade.side, trade.entry_price,
            trade.entry_time.isoformat(), trade.shares, trade.cost,
            1 if trade.resolved else 0, trade.exit_price, trade.pnl, trade.roi,
            trade.time_remaining_at_entry,
            1 if trade.binance_confirmed else (0 if trade.binance_confirmed is False else None)
        ))

        trade_id = c.lastrowid
        conn.commit()
        conn.close()

        trade.id = trade_id
        return trade_id

    def resolve_trade(self, trade_id: int, exit_price: float, pnl: float, roi: float):
        """Mark a trade as resolved."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('''
            UPDATE trades SET resolved = 1, exit_price = ?, pnl = ?, roi = ?
            WHERE id = ?
        ''', (exit_price, pnl, roi, trade_id))

        conn.commit()
        conn.close()

    def get_trade_by_market(self, market_id: str) -> Optional[SnipeTrade]:
        """Get trade for a specific market (if exists)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('SELECT * FROM trades WHERE market_id = ?', (market_id,))
        row = c.fetchone()
        conn.close()

        if row:
            return self._row_to_trade(row)
        return None

    def get_unresolved_trades(self) -> list[SnipeTrade]:
        """Get all unresolved trades."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('SELECT * FROM trades WHERE resolved = 0')
        rows = c.fetchall()
        conn.close()

        return [self._row_to_trade(row) for row in rows]

    def get_all_trades(self, limit: int = 100) -> list[SnipeTrade]:
        """Get recent trades."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT ?', (limit,))
        rows = c.fetchall()
        conn.close()

        return [self._row_to_trade(row) for row in rows]

    def get_stats(self) -> dict:
        """Get aggregate statistics."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # Total trades
        c.execute('SELECT COUNT(*) FROM trades')
        total_trades = c.fetchone()[0]

        # Resolved trades
        c.execute('SELECT COUNT(*) FROM trades WHERE resolved = 1')
        resolved_trades = c.fetchone()[0]

        # Wins (exit_price = 1.0)
        c.execute('SELECT COUNT(*) FROM trades WHERE resolved = 1 AND exit_price = 1.0')
        wins = c.fetchone()[0]

        # Total P&L
        c.execute('SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE resolved = 1')
        total_pnl = c.fetchone()[0]

        # Average ROI
        c.execute('SELECT COALESCE(AVG(roi), 0) FROM trades WHERE resolved = 1')
        avg_roi = c.fetchone()[0]

        # By asset
        c.execute('''
            SELECT asset, COUNT(*),
                   SUM(CASE WHEN exit_price = 1.0 THEN 1 ELSE 0 END),
                   COALESCE(SUM(pnl), 0)
            FROM trades WHERE resolved = 1 GROUP BY asset
        ''')
        by_asset = {row[0]: {"trades": row[1], "wins": row[2], "pnl": row[3]}
                    for row in c.fetchall()}

        conn.close()

        win_rate = wins / resolved_trades if resolved_trades > 0 else 0

        return {
            "total_trades": total_trades,
            "resolved_trades": resolved_trades,
            "wins": wins,
            "losses": resolved_trades - wins,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_roi": avg_roi,
            "by_asset": by_asset,
        }

    def save_opportunity(self, opp: SnipeOpportunity):
        """Log an opportunity (for analysis)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('''
            INSERT INTO opportunities (
                market_id, asset, timestamp, time_remaining,
                up_price, down_price, leader, leader_price, spread,
                entered, skip_reason, binance_price, binance_direction, binance_confirms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            opp.market_id, opp.asset, opp.timestamp.isoformat(), opp.time_remaining,
            opp.up_price, opp.down_price, opp.leader, opp.leader_price, opp.spread,
            1 if opp.entered else 0, opp.skip_reason,
            opp.binance_price, opp.binance_direction,
            1 if opp.binance_confirms else (0 if opp.binance_confirms is False else None)
        ))

        conn.commit()
        conn.close()

    def _row_to_trade(self, row) -> SnipeTrade:
        """Convert DB row to SnipeTrade."""
        return SnipeTrade(
            id=row[0],
            market_id=row[1],
            asset=row[2],
            side=row[3],
            entry_price=row[4],
            entry_time=datetime.fromisoformat(row[5]),
            shares=row[6],
            cost=row[7],
            resolved=bool(row[8]),
            exit_price=row[9],
            pnl=row[10],
            roi=row[11],
            time_remaining_at_entry=row[12],
            binance_confirmed=bool(row[13]) if row[13] is not None else None,
        )
