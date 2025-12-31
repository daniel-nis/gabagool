#!/usr/bin/env python3
"""
Late-Game Sniper - Main Entry Point.

Runs the snipe engine with real-time dashboard.

Usage:
    python -m sniper.run              # Run with dashboard
    python -m sniper.run --no-dash    # Run engine only (headless)
    python -m sniper.run --stats      # Show stats and exit
"""
import asyncio
import argparse
import sys
from threading import Thread
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sniper.engine import SnipeEngine
from sniper.dashboard import run_dashboard
from sniper.models import TradeStore
from sniper import config


def print_banner():
    """Print startup banner."""
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║                                                           ║
    ║   ██╗      █████╗ ████████╗███████╗     ██████╗  █████╗   ║
    ║   ██║     ██╔══██╗╚══██╔══╝██╔════╝    ██╔════╝ ██╔══██╗  ║
    ║   ██║     ███████║   ██║   █████╗      ██║  ███╗███████║  ║
    ║   ██║     ██╔══██║   ██║   ██╔══╝      ██║   ██║██╔══██║  ║
    ║   ███████╗██║  ██║   ██║   ███████╗    ╚██████╔╝██║  ██║  ║
    ║   ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚══════╝     ╚═════╝ ╚═╝  ╚═╝  ║
    ║                                                           ║
    ║             ███████╗███╗   ██╗██╗██████╗ ███████╗         ║
    ║             ██╔════╝████╗  ██║██║██╔══██╗██╔════╝         ║
    ║             ███████╗██╔██╗ ██║██║██████╔╝█████╗           ║
    ║             ╚════██║██║╚██╗██║██║██╔═══╝ ██╔══╝           ║
    ║             ███████║██║ ╚████║██║██║     ███████╗         ║
    ║             ╚══════╝╚═╝  ╚═══╝╚═╝╚═╝     ╚══════╝         ║
    ║                                                           ║
    ║   Paper Trading Bot for 15-min Crypto Binaries            ║
    ║   Final-seconds snipe strategy                            ║
    ║                                                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)


def print_stats():
    """Print trading statistics."""
    store = TradeStore(config.DB_PATH)
    stats = store.get_stats()

    print("\n" + "="*50)
    print("LATE-GAME SNIPER STATISTICS")
    print("="*50)

    print(f"\nTotal Trades: {stats['total_trades']}")
    print(f"Resolved: {stats['resolved_trades']}")
    print(f"Pending: {stats['total_trades'] - stats['resolved_trades']}")

    print(f"\nWins: {stats['wins']}")
    print(f"Losses: {stats['losses']}")
    print(f"Win Rate: {stats['win_rate']:.1%}")

    print(f"\nTotal P&L: ${stats['total_pnl']:+.2f}")
    print(f"Avg ROI: {stats['avg_roi']:+.1%}")

    if stats['by_asset']:
        print("\nBy Asset:")
        for asset, data in stats['by_asset'].items():
            wr = data['wins'] / data['trades'] if data['trades'] > 0 else 0
            print(f"  {asset}: {data['trades']} trades, {wr:.0%} win rate, ${data['pnl']:+.2f}")

    print("="*50 + "\n")


def run_engine_only():
    """Run engine without dashboard."""
    print_banner()
    engine = SnipeEngine()
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        print("\nShutting down...")
        engine.stop()


def run_with_dashboard():
    """Run engine with dashboard."""
    print_banner()
    engine = SnipeEngine()

    # Start engine in background thread
    def engine_loop():
        asyncio.run(engine.run())

    engine_thread = Thread(target=engine_loop, daemon=True)
    engine_thread.start()

    # Run dashboard in main thread (Flask)
    try:
        run_dashboard(engine)
    except KeyboardInterrupt:
        print("\nShutting down...")
        engine.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Late-Game Sniper - Paper trading bot for 15-min crypto binaries"
    )
    parser.add_argument(
        "--no-dash", action="store_true",
        help="Run without dashboard (headless mode)"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show statistics and exit"
    )
    parser.add_argument(
        "--port", type=int, default=config.DASHBOARD_PORT,
        help=f"Dashboard port (default: {config.DASHBOARD_PORT})"
    )

    args = parser.parse_args()

    if args.stats:
        print_stats()
        return

    if args.port != config.DASHBOARD_PORT:
        config.DASHBOARD_PORT = args.port

    if args.no_dash:
        run_engine_only()
    else:
        run_with_dashboard()


if __name__ == "__main__":
    main()
