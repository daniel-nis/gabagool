#!/usr/bin/env python3
"""
Gabagool - Paper Trading Bot for Polymarket 15-min Crypto Binaries.

Usage:
    python run.py                      # Run with defaults
    python run.py --dashboard          # Run with web dashboard
    python run.py --trade-size 20      # Custom trade size
    python run.py --dip 0.03           # 3% dip threshold
    python run.py --assets BTC ETH SOL # Trade specific assets

Examples:
    # Paper trade BTC and ETH with $10 trades, 5% dip threshold
    python run.py

    # Paper trade with dashboard
    python run.py --dashboard

    # Custom configuration
    python run.py --capital 5000 --trade-size 25 --dip 0.07 --dashboard
"""
import asyncio
import argparse
import threading
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import GabagoolEngine, EngineConfig


def main():
    parser = argparse.ArgumentParser(
        description="Gabagool - Polymarket 15-min Crypto Binary Paper Trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py                        # Run with defaults
  python run.py --dashboard            # With web dashboard
  python run.py --trade-size 20        # $20 per trade
  python run.py --dip 0.03             # 3% dip threshold
  python run.py --assets BTC ETH SOL   # Trade specific assets
        """
    )

    # Trading parameters
    parser.add_argument(
        '--capital', type=float, default=1000.0,
        help='Starting capital (default: $1000)'
    )
    parser.add_argument(
        '--trade-size', type=float, default=10.0,
        help='$ amount per trade (default: $10)'
    )
    parser.add_argument(
        '--dip', type=float, default=0.05,
        help='Dip threshold as decimal (default: 0.05 = 5%%)'
    )
    parser.add_argument(
        '--lookback', type=int, default=20,
        help='MA lookback periods (default: 20)'
    )
    parser.add_argument(
        '--max-position', type=float, default=100.0,
        help='Max $ per side per market (default: $100)'
    )
    parser.add_argument(
        '--assets', nargs='+', default=['BTC', 'ETH'],
        help='Assets to trade (default: BTC ETH)'
    )

    # Dashboard
    parser.add_argument(
        '--dashboard', action='store_true',
        help='Enable web dashboard'
    )
    parser.add_argument(
        '--port', type=int, default=5050,
        help='Dashboard port (default: 5050)'
    )

    # Persistence
    parser.add_argument(
        '--db', type=str, default='data/gabagool.db',
        help='SQLite database path'
    )
    parser.add_argument(
        '--csv', type=str, default='logs/trades.csv',
        help='Trade log CSV path'
    )

    args = parser.parse_args()

    # Print banner
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║                                                           ║
    ║   ██████   █████  ██████   █████   ██████   ██████  ██    ║
    ║  ██       ██   ██ ██   ██ ██   ██ ██       ██    ██ ██    ║
    ║  ██   ███ ███████ ██████  ███████ ██   ███ ██    ██ ██    ║
    ║  ██    ██ ██   ██ ██   ██ ██   ██ ██    ██ ██    ██ ██    ║
    ║   ██████  ██   ██ ██████  ██   ██  ██████   ██████  ██████║
    ║                                                           ║
    ║   Polymarket 15-min Crypto Binary Paper Trading           ║
    ║   Asymmetric scalping for locked profit                   ║
    ║                                                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    print(f"  Configuration:")
    print(f"    Starting Capital: ${args.capital:,.2f}")
    print(f"    Trade Size:       ${args.trade_size:.2f}")
    print(f"    Dip Threshold:    {args.dip*100:.1f}%")
    print(f"    MA Lookback:      {args.lookback} periods")
    print(f"    Max Position:     ${args.max_position:.2f}/side")
    print(f"    Assets:           {', '.join(args.assets)}")
    print(f"    Database:         {args.db}")
    print(f"    Trade Log:        {args.csv}")
    if args.dashboard:
        print(f"    Dashboard:        http://localhost:{args.port}")
    print()

    # Create config
    config = EngineConfig(
        starting_capital=args.capital,
        trade_size=args.trade_size,
        dip_threshold=args.dip,
        lookback_periods=args.lookback,
        max_position_per_side=args.max_position,
        assets=args.assets,
        db_path=args.db,
        csv_path=args.csv,
    )

    # Dashboard callbacks
    on_state_update = None
    on_trade = None

    if args.dashboard:
        from dashboard import (
            run_dashboard,
            update_dashboard_state,
            emit_trade,
        )

        on_state_update = update_dashboard_state
        on_trade = emit_trade

        # Start dashboard in background
        dashboard_thread = threading.Thread(
            target=run_dashboard,
            kwargs={'port': args.port},
            daemon=True
        )
        dashboard_thread.start()

        import time
        time.sleep(1)  # Give dashboard time to start

    # Create and run engine
    engine = GabagoolEngine(
        config=config,
        on_state_update=on_state_update,
        on_trade=on_trade,
    )

    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        print("\n\nStopping...")
        engine.stop()


if __name__ == "__main__":
    main()
