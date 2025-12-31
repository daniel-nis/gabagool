"""
Late-Game Sniper Dashboard.

Real-time monitoring dashboard for the snipe strategy.
"""
import asyncio
from datetime import datetime, timezone
from threading import Thread
from flask import Flask, render_template_string
from flask_socketio import SocketIO

from sniper.engine import SnipeEngine, MarketState
from sniper.models import SnipeTrade, SnipeOpportunity, TradeStore
from sniper import config

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sniper-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global engine reference
engine: SnipeEngine = None

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Late-Game Sniper</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'SF Mono', 'Consolas', monospace;
            background: #0a0a0a;
            color: #e0e0e0;
            padding: 20px;
        }
        h1 {
            color: #ff6b35;
            margin-bottom: 20px;
            font-size: 24px;
        }
        h2 {
            color: #888;
            font-size: 14px;
            text-transform: uppercase;
            margin-bottom: 10px;
            letter-spacing: 1px;
        }
        .container { max-width: 1400px; margin: 0 auto; }

        /* Header Stats */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 15px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: #1a1a1a;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
        }
        .stat-value {
            font-size: 32px;
            font-weight: bold;
            color: #fff;
        }
        .stat-value.positive { color: #4ade80; }
        .stat-value.negative { color: #f87171; }
        .stat-label {
            color: #888;
            font-size: 12px;
            margin-top: 5px;
        }

        /* Markets Panel */
        .panel {
            background: #1a1a1a;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .markets-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 15px;
        }
        .market-card {
            background: #252525;
            border: 1px solid #444;
            border-radius: 8px;
            padding: 15px;
            transition: all 0.3s;
        }
        .market-card.snipe-zone {
            border-color: #ff6b35;
            box-shadow: 0 0 20px rgba(255, 107, 53, 0.3);
        }
        .market-card.can-enter {
            border-color: #4ade80;
            box-shadow: 0 0 20px rgba(74, 222, 128, 0.3);
            animation: pulse 1s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }
        .market-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .market-asset {
            font-size: 20px;
            font-weight: bold;
        }
        .market-timer {
            font-size: 24px;
            font-weight: bold;
            font-family: 'SF Mono', monospace;
        }
        .timer-danger { color: #f87171; }
        .timer-warning { color: #fbbf24; }
        .timer-normal { color: #888; }
        .prices {
            display: flex;
            justify-content: space-between;
            margin-bottom: 10px;
        }
        .price-side {
            text-align: center;
            flex: 1;
        }
        .price-label { color: #888; font-size: 12px; }
        .price-value {
            font-size: 20px;
            font-weight: bold;
        }
        .price-up { color: #4ade80; }
        .price-down { color: #f87171; }
        .market-status {
            text-align: center;
            padding: 8px;
            border-radius: 4px;
            font-size: 12px;
            margin-top: 10px;
        }
        .status-waiting { background: #333; color: #888; }
        .status-snipe-zone { background: #ff6b35; color: #000; }
        .status-can-enter { background: #4ade80; color: #000; font-weight: bold; }
        .status-traded { background: #3b82f6; color: #fff; }
        .status-skipped { background: #6b7280; color: #fff; }

        /* Trade Log */
        .trade-log {
            max-height: 400px;
            overflow-y: auto;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #333;
        }
        th {
            color: #888;
            font-weight: normal;
            font-size: 12px;
            text-transform: uppercase;
        }
        tr.win td { color: #4ade80; }
        tr.loss td { color: #f87171; }
        tr.pending td { color: #fbbf24; }

        /* Two column layout */
        .two-col {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }

        /* ROI indicator */
        .roi-bar {
            height: 4px;
            background: #333;
            border-radius: 2px;
            margin-top: 5px;
            overflow: hidden;
        }
        .roi-fill {
            height: 100%;
            background: linear-gradient(90deg, #ff6b35, #4ade80);
            transition: width 0.3s;
        }

        /* Binance indicator */
        .binance-confirm {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 10px;
            margin-left: 5px;
        }
        .binance-yes { background: #4ade80; color: #000; }
        .binance-no { background: #f87171; color: #000; }
        .binance-na { background: #333; color: #888; }
    </style>
</head>
<body>
    <div class="container">
        <h1>LATE-GAME SNIPER</h1>

        <!-- Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value" id="total-pnl">$0.00</div>
                <div class="stat-label">Total P&L</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="win-rate">0%</div>
                <div class="stat-label">Win Rate</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="avg-roi">0%</div>
                <div class="stat-label">Avg ROI</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="total-trades">0</div>
                <div class="stat-label">Total Trades</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="active-positions">0</div>
                <div class="stat-label">Active Positions</div>
            </div>
        </div>

        <!-- Active Markets -->
        <div class="panel">
            <h2>Active Markets</h2>
            <div class="markets-grid" id="markets-container">
                <div class="market-card">
                    <div style="text-align: center; color: #888; padding: 40px;">
                        Waiting for markets...
                    </div>
                </div>
            </div>
        </div>

        <!-- Trade Log & Analytics -->
        <div class="two-col">
            <div class="panel">
                <h2>Trade Log</h2>
                <div class="trade-log">
                    <table>
                        <thead>
                            <tr>
                                <th>Asset</th>
                                <th>Side</th>
                                <th>Entry</th>
                                <th>Exit</th>
                                <th>P&L</th>
                                <th>ROI</th>
                            </tr>
                        </thead>
                        <tbody id="trade-log">
                            <tr><td colspan="6" style="text-align:center;color:#888;">No trades yet</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="panel">
                <h2>Recent Opportunities</h2>
                <div class="trade-log">
                    <table>
                        <thead>
                            <tr>
                                <th>Asset</th>
                                <th>Leader</th>
                                <th>Price</th>
                                <th>Time Left</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="opportunity-log">
                            <tr><td colspan="5" style="text-align:center;color:#888;">No opportunities yet</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let serverTimeOffset = 0;

        socket.on('connect', () => {
            console.log('Connected to server');
            socket.emit('request_state');
        });

        socket.on('stats', (stats) => {
            const pnl = stats.total_pnl || 0;
            const pnlEl = document.getElementById('total-pnl');
            pnlEl.textContent = '$' + pnl.toFixed(2);
            pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'positive' : 'negative');

            document.getElementById('win-rate').textContent = ((stats.win_rate || 0) * 100).toFixed(0) + '%';
            document.getElementById('avg-roi').textContent = ((stats.avg_roi || 0) * 100).toFixed(1) + '%';
            document.getElementById('total-trades').textContent = stats.resolved_trades || 0;
            document.getElementById('active-positions').textContent = stats.total_trades - (stats.resolved_trades || 0);
        });

        socket.on('markets', (markets) => {
            const container = document.getElementById('markets-container');

            if (Object.keys(markets).length === 0) {
                container.innerHTML = '<div class="market-card"><div style="text-align:center;color:#888;padding:40px;">Waiting for markets...</div></div>';
                return;
            }

            let html = '';
            for (const [id, m] of Object.entries(markets)) {
                const timerClass = m.time_remaining < 30 ? 'timer-danger' : m.time_remaining < 60 ? 'timer-warning' : 'timer-normal';
                let cardClass = 'market-card';
                let statusClass = 'status-waiting';
                let statusText = 'Waiting';

                if (m.traded) {
                    cardClass += '';
                    statusClass = 'status-traded';
                    statusText = 'TRADED';
                } else if (m.can_enter) {
                    cardClass += ' can-enter';
                    statusClass = 'status-can-enter';
                    statusText = 'SNIPE NOW!';
                } else if (m.in_snipe_zone) {
                    cardClass += ' snipe-zone';
                    statusClass = 'status-snipe-zone';
                    statusText = m.skip_reason || 'In Zone';
                }

                const binanceClass = m.binance_confirms === true ? 'binance-yes' :
                                    m.binance_confirms === false ? 'binance-no' : 'binance-na';
                const binanceText = m.binance_confirms === true ? 'Confirmed' :
                                   m.binance_confirms === false ? 'Divergent' : 'N/A';

                html += `
                <div class="${cardClass}">
                    <div class="market-header">
                        <span class="market-asset">${m.asset}</span>
                        <span class="market-timer ${timerClass}">${formatTime(m.time_remaining)}</span>
                    </div>
                    <div class="prices">
                        <div class="price-side">
                            <div class="price-label">UP</div>
                            <div class="price-value price-up">${(m.up_price * 100).toFixed(1)}%</div>
                        </div>
                        <div class="price-side">
                            <div class="price-label">DOWN</div>
                            <div class="price-value price-down">${(m.down_price * 100).toFixed(1)}%</div>
                        </div>
                    </div>
                    ${m.leader ? `
                    <div style="text-align:center;margin:10px 0;">
                        <span style="color:#888;">Leader:</span>
                        <span style="color:${m.leader === 'UP' ? '#4ade80' : '#f87171'};font-weight:bold;">${m.leader}</span>
                        <span class="binance-confirm ${binanceClass}">${binanceText}</span>
                    </div>
                    ${m.expected_roi ? `
                    <div style="text-align:center;font-size:12px;color:#888;">
                        Expected ROI: <span style="color:#ff6b35;font-weight:bold;">${(m.expected_roi * 100).toFixed(1)}%</span>
                    </div>
                    ` : ''}
                    ` : ''}
                    <div class="market-status ${statusClass}">${statusText}</div>
                </div>
                `;
            }
            container.innerHTML = html;
        });

        socket.on('trades', (trades) => {
            const tbody = document.getElementById('trade-log');
            if (trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;">No trades yet</td></tr>';
                return;
            }

            let html = '';
            for (const t of trades) {
                const rowClass = t.resolved ? (t.exit_price === 1.0 ? 'win' : 'loss') : 'pending';
                html += `
                <tr class="${rowClass}">
                    <td>${t.asset}</td>
                    <td>${t.side}</td>
                    <td>${(t.entry_price * 100).toFixed(1)}%</td>
                    <td>${t.resolved ? (t.exit_price === 1.0 ? 'WIN' : 'LOSS') : 'Pending'}</td>
                    <td>${t.resolved ? '$' + t.pnl.toFixed(2) : '-'}</td>
                    <td>${t.resolved ? (t.roi * 100).toFixed(1) + '%' : '-'}</td>
                </tr>
                `;
            }
            tbody.innerHTML = html;
        });

        socket.on('opportunity', (opp) => {
            const tbody = document.getElementById('opportunity-log');
            const first = tbody.querySelector('tr');
            if (first && first.querySelector('td[colspan]')) {
                tbody.innerHTML = '';
            }

            const actionClass = opp.entered ? 'style="color:#4ade80;font-weight:bold;"' : 'style="color:#888;"';
            const actionText = opp.entered ? 'ENTERED' : (opp.skip_reason || 'Skipped');

            const row = document.createElement('tr');
            row.innerHTML = `
                <td>${opp.asset}</td>
                <td>${opp.leader}</td>
                <td>${(opp.leader_price * 100).toFixed(1)}%</td>
                <td>${opp.time_remaining.toFixed(0)}s</td>
                <td ${actionClass}>${actionText}</td>
            `;
            tbody.insertBefore(row, tbody.firstChild);

            // Keep only last 20
            while (tbody.children.length > 20) {
                tbody.removeChild(tbody.lastChild);
            }
        });

        function formatTime(seconds) {
            if (seconds < 0) return '0:00';
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return mins + ':' + secs.toString().padStart(2, '0');
        }

        // Request periodic updates
        setInterval(() => {
            socket.emit('request_state');
        }, 500);
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)


@socketio.on('connect')
def handle_connect():
    emit_state()


@socketio.on('request_state')
def handle_request_state():
    emit_state()


def emit_state():
    """Emit current state to client."""
    global engine
    if not engine:
        return

    # Stats
    stats = engine.get_stats()
    socketio.emit('stats', stats)

    # Markets
    markets_data = {}
    for cid, state in engine.get_all_states().items():
        markets_data[cid] = {
            'asset': state.market.asset,
            'time_remaining': state.time_remaining,
            'up_price': state.up_price,
            'down_price': state.down_price,
            'in_snipe_zone': state.in_snipe_zone,
            'can_enter': state.can_enter,
            'leader': state.leader,
            'leader_price': state.leader_price,
            'expected_roi': state.expected_roi,
            'skip_reason': state.skip_reason,
            'binance_confirms': state.price_confirms,
            'traded': cid in engine.traded_markets,
        }
    socketio.emit('markets', markets_data)

    # Recent trades
    trades = engine.get_recent_trades(20)
    trades_data = [{
        'asset': t.asset,
        'side': t.side,
        'entry_price': t.entry_price,
        'exit_price': t.exit_price,
        'pnl': t.pnl,
        'roi': t.roi,
        'resolved': t.resolved,
    } for t in trades]
    socketio.emit('trades', trades_data)


def emit_opportunity(opp: SnipeOpportunity):
    """Emit new opportunity to clients."""
    socketio.emit('opportunity', {
        'asset': opp.asset,
        'leader': opp.leader,
        'leader_price': opp.leader_price,
        'time_remaining': opp.time_remaining,
        'entered': opp.entered,
        'skip_reason': opp.skip_reason,
    })


def run_dashboard(snipe_engine: SnipeEngine):
    """Run the dashboard server."""
    global engine
    engine = snipe_engine

    # Register callbacks
    engine.on_update(lambda _: emit_state())
    engine.on_trade(lambda _: emit_state())
    engine.on_opportunity(emit_opportunity)

    print(f"\nDashboard running at http://localhost:{config.DASHBOARD_PORT}")
    socketio.run(app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    # Test mode - create engine and run
    from sniper.engine import SnipeEngine

    engine = SnipeEngine()

    # Run engine in background thread
    def run_engine():
        asyncio.run(engine.run())

    engine_thread = Thread(target=run_engine, daemon=True)
    engine_thread.start()

    run_dashboard(engine)
