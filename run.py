#!/usr/bin/env python3
"""
Gabagool v4 - Opportunistic Dip-Buying for Polymarket 15-min Crypto Binaries.

Strategy: Buy YES and NO dips independently, match pairs to lock profit.

Usage:
    python run.py                        # Run with defaults
    python run.py --dashboard            # Run with web dashboard
    python run.py --yes-threshold 0.45   # Buy YES below $0.45
    python run.py --trade-size 20        # $20 per buy
    python run.py --assets BTC ETH SOL   # Trade specific assets

Examples:
    # Paper trade BTC and ETH with default settings
    python run.py

    # Paper trade with dashboard
    python run.py --dashboard

    # Custom configuration
    python run.py --capital 5000 --trade-size 25 --yes-threshold 0.45 --dashboard
"""
import asyncio
import argparse
import threading
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import GabagoolEngine, EngineConfig


def main():
    parser = argparse.ArgumentParser(
        description="Gabagool v4 - Opportunistic Dip-Buying for Polymarket 15-min Crypto Binaries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py                          # Run with defaults
  python run.py --dashboard              # With web dashboard
  python run.py --trade-size 20          # $20 per buy
  python run.py --yes-threshold 0.45     # Buy YES below $0.45
  python run.py --assets BTC ETH SOL     # Trade specific assets
        """
    )

    # Capital
    parser.add_argument(
        '--capital', type=float, default=1000.0,
        help='Starting capital (default: $1000)'
    )

    # v4 Dip-buy parameters
    parser.add_argument(
        '--yes-threshold', type=float, default=0.48,
        help='Buy YES when price below this (default: 0.48)'
    )
    parser.add_argument(
        '--no-threshold', type=float, default=0.48,
        help='Buy NO when price below this (default: 0.48)'
    )
    parser.add_argument(
        '--use-ma', action='store_true', default=True,
        help='Use moving average for dip detection (default: True)'
    )
    parser.add_argument(
        '--no-ma', action='store_true',
        help='Disable moving average dip detection'
    )
    parser.add_argument(
        '--ma-window', type=float, default=30.0,
        help='Moving average window in seconds (default: 30)'
    )
    parser.add_argument(
        '--dip-pct', type=float, default=0.03,
        help='Buy when price is this %% below MA (default: 0.03 = 3%%)'
    )

    # Position sizing
    parser.add_argument(
        '--trade-size', type=float, default=10.0,
        help='$ amount per trade (default: $10)'
    )
    parser.add_argument(
        '--max-unmatched', type=float, default=50.0,
        help='Max $ unmatched per side per market (default: $50)'
    )
    parser.add_argument(
        '--max-position', type=float, default=200.0,
        help='Max $ total per market (default: $200)'
    )

    # Timing
    parser.add_argument(
        '--cooldown', type=float, default=5.0,
        help='Seconds between trades on same side (default: 5)'
    )
    parser.add_argument(
        '--close-before', type=float, default=2.0,
        help='Close unmatched this many minutes before expiry (default: 2)'
    )

    # Assets
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

    # Live trading
    parser.add_argument(
        '--live', action='store_true',
        help='Enable live trading (requires POLYMARKET_PRIVATE_KEY env var)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='With --live: sign orders but do not submit (test mode)'
    )

    args = parser.parse_args()

    # Handle --no-ma flag
    use_ma = args.use_ma and not args.no_ma

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
    ║   Polymarket 15-min Crypto Binary Trading                  ║
    ║   v4: DIP-BUY - Buy dips, match pairs, lock profit        ║
    ║                                                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    # Determine trading mode
    trading_mode = "PAPER"
    if args.live:
        trading_mode = "DRY-RUN" if args.dry_run else "LIVE"

    print(f"  Mode: {trading_mode}")
    print(f"  Configuration (v4 Dip-Buy):")
    print(f"    Starting Capital: ${args.capital:,.2f}")
    print(f"    Trade Size:       ${args.trade_size:.2f}")
    print(f"    YES Threshold:    ${args.yes_threshold:.2f}")
    print(f"    NO Threshold:     ${args.no_threshold:.2f}")
    print(f"    Use MA:           {use_ma}")
    if use_ma:
        print(f"    MA Window:        {args.ma_window}s")
        print(f"    Dip Below MA:     {args.dip_pct*100:.1f}%")
    print(f"    Max Unmatched:    ${args.max_unmatched:.2f}/side")
    print(f"    Max Position:     ${args.max_position:.2f}/market")
    print(f"    Cooldown:         {args.cooldown}s")
    print(f"    Close Before:     {args.close_before}min")
    print(f"    Assets:           {', '.join(args.assets)}")
    print(f"    Database:         {args.db}")
    print(f"    Trade Log:        {args.csv}")
    if args.dashboard:
        print(f"    Dashboard:        http://localhost:{args.port}")
    print()

    # Create config
    config = EngineConfig(
        starting_capital=args.capital,
        yes_buy_threshold=args.yes_threshold,
        no_buy_threshold=args.no_threshold,
        use_moving_average=use_ma,
        ma_window_seconds=args.ma_window,
        dip_below_ma_pct=args.dip_pct,
        trade_size=args.trade_size,
        max_unmatched_cost=args.max_unmatched,
        max_position_cost=args.max_position,
        cooldown_seconds=args.cooldown,
        close_before_expiry_mins=args.close_before,
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

        dashboard_thread = threading.Thread(
            target=run_dashboard,
            kwargs={'port': args.port},
            daemon=True
        )
        dashboard_thread.start()

        import time
        time.sleep(1)

    # Create execution backend
    execution_backend = None
    if args.live:
        try:
            from helpers.wallet import Wallet
            from helpers.clob_trading import CLOBClient
            from helpers.risk_manager import RiskManager, RiskConfig
            from engine import LiveExecutionBackend

            print("  Initializing live trading...")
            wallet = Wallet.from_env()
            print(f"    Wallet: {wallet.address}")

            # Initialize risk manager with configured limits
            risk_config = RiskConfig(
                min_balance=50.0,
                daily_loss_limit=100.0,
                max_position_per_market=args.max_position,
                max_total_exposure=args.max_position * 2,
                max_trade_size=args.trade_size * 3,
            )
            risk_manager = RiskManager(risk_config)
            print(f"    Risk Limits: ${risk_config.daily_loss_limit}/day, ${risk_config.max_total_exposure} max exposure")

            clob = CLOBClient(wallet, dry_run=args.dry_run)
            execution_backend = LiveExecutionBackend(
                config=config,
                wallet=wallet,
                clob_client=clob,
                risk_manager=risk_manager,
                dry_run=args.dry_run,
            )
            print(f"    Status: {'DRY-RUN (signing only)' if args.dry_run else 'LIVE TRADING ENABLED'}")
            print()

            if not args.dry_run:
                print("  ⚠️  WARNING: LIVE TRADING MODE - REAL MONEY AT RISK!")
                print("  Press Ctrl+C within 5 seconds to cancel...")
                import time
                time.sleep(5)
                print("  Starting live trading...")
                print()

        except ImportError as e:
            print(f"  ❌ Live trading requires additional packages: {e}")
            print("     Run: pip install web3 eth-account httpx")
            return
        except ValueError as e:
            print(f"  ❌ Wallet error: {e}")
            print("     Set POLYMARKET_PRIVATE_KEY environment variable")
            return
        except Exception as e:
            print(f"  ❌ Live trading setup failed: {e}")
            return

    # Create and run engine
    engine = GabagoolEngine(
        config=config,
        execution_backend=execution_backend,
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
