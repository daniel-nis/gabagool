#!/usr/bin/env python3
"""
Gabagool Dashboard - Real-time monitoring + Analytics for paper trading.

Features:
- Live YES/NO price charts per active market
- Position tracking with unrealized P&L
- Locked profit tracking
- Analytics tab with performance reports
- Trade history and analysis
"""
import threading
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from collections import deque
from pathlib import Path

from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO


# Global state
class DashboardState:
    def __init__(self):
        self.capital = 1000.0
        self.total_pnl = 0.0
        self.trade_count = 0
        self.markets_completed = 0
        self.total_locked_profit = 0.0
        self.total_exposure = 0.0
        self.markets: Dict[str, dict] = {}
        self.positions: Dict[str, dict] = {}
        self.trades: List[dict] = []
        self.price_history: Dict[str, deque] = {}
        self.trade_markers: Dict[str, List[dict]] = {}
        self.last_update = None
        self.start_time = datetime.now(timezone.utc)

        # Performance tracking
        self.pnl_history: List[dict] = []  # [{time, pnl, locked}, ...]

        self.config = {
            'starting_capital': 1000.0,
            'trade_size': 10.0,
            'dip_threshold': 0.05,
            'assets': ['BTC', 'ETH'],
        }

dashboard_state = DashboardState()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'gabagool-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gabagool</title>
    <script src="https://cdn.socket.io/4.0.0/socket.io.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #0a0a0a;
            --surface: #111;
            --surface2: #1a1a1a;
            --border: #252525;
            --fg: #e8e8e8;
            --dim: #888;
            --muted: #555;
            --green: #22c55e;
            --green-dim: rgba(34, 197, 94, 0.15);
            --red: #ef4444;
            --red-dim: rgba(239, 68, 68, 0.15);
            --blue: #3b82f6;
            --blue-dim: rgba(59, 130, 246, 0.15);
            --yellow: #eab308;
            --purple: #a855f7;
        }
        html, body {
            font-family: 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Code', monospace;
            font-size: 12px;
            background: var(--bg);
            color: var(--fg);
            height: 100vh;
            overflow: hidden;
        }
        .app {
            display: grid;
            grid-template-rows: 56px 1fr;
            height: 100vh;
        }

        /* Header */
        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            border-bottom: 1px solid var(--border);
            background: var(--surface);
        }
        .header-left {
            display: flex;
            align-items: center;
            gap: 24px;
        }
        .logo {
            font-size: 18px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--green), var(--blue));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .status {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--muted);
        }
        .dot.live { background: var(--green); box-shadow: 0 0 10px var(--green); }
        .status-text { color: var(--dim); font-size: 11px; }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 4px;
            margin-left: 20px;
        }
        .tab {
            padding: 8px 16px;
            background: transparent;
            border: 1px solid transparent;
            border-radius: 6px;
            color: var(--dim);
            cursor: pointer;
            font-family: inherit;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            transition: all 0.2s;
        }
        .tab:hover {
            color: var(--fg);
            background: var(--surface2);
        }
        .tab.active {
            color: var(--fg);
            background: var(--surface2);
            border-color: var(--border);
        }

        .header-metrics {
            display: flex;
            gap: 40px;
        }
        .metric {
            text-align: center;
        }
        .metric-val {
            font-size: 18px;
            font-weight: 600;
        }
        .metric-val.pos { color: var(--green); }
        .metric-val.neg { color: var(--red); }
        .metric-lbl {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 2px;
        }

        .header-right {
            display: flex;
            align-items: center;
            gap: 16px;
        }
        .runtime {
            color: var(--dim);
            font-size: 11px;
        }
        .clock { color: var(--dim); font-size: 11px; }

        /* Main content */
        .main {
            display: grid;
            grid-template-columns: 1fr 300px;
            overflow: hidden;
        }
        .main.full-width {
            grid-template-columns: 1fr;
        }

        .content {
            padding: 20px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        /* Section */
        .section {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
            background: var(--surface2);
        }
        .section-title {
            font-size: 11px;
            color: var(--dim);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .section-body {
            padding: 16px;
        }

        /* Markets grid with charts */
        .markets-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 16px;
        }
        .market-card {
            background: var(--surface2);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
        }
        .market-card.has-position {
            border-color: var(--blue);
        }
        .market-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
        }
        .market-title {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .market-asset {
            font-size: 16px;
            font-weight: 600;
        }
        .market-time {
            font-size: 12px;
            color: var(--dim);
            padding: 4px 8px;
            background: var(--bg);
            border-radius: 4px;
        }
        .market-time.urgent { color: var(--red); background: var(--red-dim); }

        .market-prices {
            display: flex;
            gap: 12px;
        }
        .price-badge {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 14px;
            font-weight: 600;
        }
        .price-badge.yes { background: var(--green-dim); color: var(--green); }
        .price-badge.no { background: var(--red-dim); color: var(--red); }
        .price-badge .label { font-size: 10px; opacity: 0.8; }

        .chart-container {
            height: 160px;
            padding: 12px;
            position: relative;
        }

        .market-position {
            padding: 12px 16px;
            border-top: 1px solid var(--border);
            background: var(--bg);
        }
        .position-grid {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 12px;
        }
        .position-item {
            text-align: center;
        }
        .position-value {
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 2px;
        }
        .position-value.pos { color: var(--green); }
        .position-value.neg { color: var(--red); }
        .position-label {
            font-size: 9px;
            color: var(--muted);
            text-transform: uppercase;
        }

        .no-position {
            text-align: center;
            color: var(--muted);
            font-size: 11px;
            padding: 12px;
        }

        /* Summary cards */
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 12px;
        }
        .summary-card {
            background: var(--surface2);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 16px;
            text-align: center;
        }
        .summary-value {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 4px;
        }
        .summary-value.pos { color: var(--green); }
        .summary-value.neg { color: var(--red); }
        .summary-label {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
        }

        /* Analytics styles */
        .analytics-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
        }
        .analytics-card {
            background: var(--surface2);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
        }
        .analytics-card.wide {
            grid-column: span 2;
        }
        .analytics-card.full {
            grid-column: span 4;
        }
        .analytics-title {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            margin-bottom: 12px;
            letter-spacing: 0.5px;
        }
        .analytics-value {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 4px;
        }
        .analytics-value.pos { color: var(--green); }
        .analytics-value.neg { color: var(--red); }
        .analytics-sub {
            font-size: 11px;
            color: var(--dim);
        }
        .analytics-chart {
            height: 200px;
        }

        /* Stats table */
        .stats-table {
            width: 100%;
            border-collapse: collapse;
        }
        .stats-table th,
        .stats-table td {
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }
        .stats-table th {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            font-weight: 500;
        }
        .stats-table td {
            font-size: 12px;
        }
        .stats-table tr:last-child td {
            border-bottom: none;
        }

        /* Progress bar */
        .progress-bar {
            height: 8px;
            background: var(--border);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 8px;
        }
        .progress-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s;
        }
        .progress-fill.green { background: var(--green); }
        .progress-fill.red { background: var(--red); }
        .progress-fill.blue { background: var(--blue); }

        /* Sidebar */
        .sidebar {
            background: var(--surface);
            border-left: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .sidebar-section {
            padding: 16px;
            border-bottom: 1px solid var(--border);
        }
        .sidebar-section.trades-section {
            flex: 1;
            overflow-y: auto;
            border-bottom: none;
        }
        .sidebar-title {
            font-size: 10px;
            color: var(--muted);
            text-transform: uppercase;
            margin-bottom: 12px;
            letter-spacing: 0.5px;
        }

        .config-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .config-label {
            font-size: 11px;
            color: var(--dim);
        }
        .config-input {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 6px 10px;
            font-size: 12px;
            color: var(--fg);
            width: 70px;
            text-align: right;
            font-family: inherit;
        }
        .config-input:focus {
            outline: none;
            border-color: var(--blue);
        }

        .trades-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .trade-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 12px;
            background: var(--bg);
            border-radius: 6px;
            border-left: 3px solid transparent;
        }
        .trade-item.yes { border-left-color: var(--green); }
        .trade-item.no { border-left-color: var(--red); }
        .trade-side {
            font-size: 10px;
            font-weight: 600;
            padding: 3px 8px;
            border-radius: 3px;
            text-transform: uppercase;
            min-width: 36px;
            text-align: center;
        }
        .trade-side.yes { background: var(--green-dim); color: var(--green); }
        .trade-side.no { background: var(--red-dim); color: var(--red); }
        .trade-info { flex: 1; }
        .trade-asset { font-size: 12px; font-weight: 500; }
        .trade-time { font-size: 10px; color: var(--muted); }
        .trade-details { text-align: right; }
        .trade-price { font-size: 12px; }
        .trade-shares { font-size: 10px; color: var(--dim); }

        .empty-state {
            text-align: center;
            padding: 30px;
            color: var(--muted);
            font-size: 12px;
        }

        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

        @media (max-width: 1200px) {
            .main { grid-template-columns: 1fr; }
            .sidebar { display: none; }
            .markets-grid { grid-template-columns: 1fr; }
            .summary-grid { grid-template-columns: repeat(3, 1fr); }
            .analytics-grid { grid-template-columns: repeat(2, 1fr); }
            .analytics-card.wide { grid-column: span 1; }
            .analytics-card.full { grid-column: span 2; }
        }
    </style>
</head>
<body>
    <div class="app">
        <header class="header">
            <div class="header-left">
                <span class="logo">GABAGOOL</span>
                <div class="status">
                    <span class="dot" id="status-dot"></span>
                    <span class="status-text" id="status-text">connecting...</span>
                </div>
                <div class="tabs">
                    <button class="tab active" data-tab="trading">Trading</button>
                    <button class="tab" data-tab="analytics">Analytics</button>
                </div>
            </div>
            <div class="header-metrics">
                <div class="metric">
                    <div class="metric-val" id="total-pnl">$0.00</div>
                    <div class="metric-lbl">Realized</div>
                </div>
                <div class="metric">
                    <div class="metric-val pos" id="locked-profit">$0.00</div>
                    <div class="metric-lbl">Locked</div>
                </div>
                <div class="metric">
                    <div class="metric-val" id="trade-count">0</div>
                    <div class="metric-lbl">Trades</div>
                </div>
            </div>
            <div class="header-right">
                <div class="runtime" id="runtime">Runtime: 0:00:00</div>
                <div class="clock" id="clock">00:00:00</div>
            </div>
        </header>

        <div class="main" id="main-container">
            <div class="content">
                <!-- Trading Tab -->
                <div class="tab-content active" id="trading-tab">
                    <div class="summary-grid">
                        <div class="summary-card">
                            <div class="summary-value" id="capital">$1,000</div>
                            <div class="summary-label">Capital</div>
                        </div>
                        <div class="summary-card">
                            <div class="summary-value" id="total-invested">$0</div>
                            <div class="summary-label">Invested</div>
                        </div>
                        <div class="summary-card">
                            <div class="summary-value pos" id="total-locked">$0.00</div>
                            <div class="summary-label">Locked</div>
                        </div>
                        <div class="summary-card">
                            <div class="summary-value" id="total-exposure">$0.00</div>
                            <div class="summary-label">At Risk</div>
                        </div>
                        <div class="summary-card">
                            <div class="summary-value" id="matched-pct">0%</div>
                            <div class="summary-label">Matched</div>
                        </div>
                        <div class="summary-card">
                            <div class="summary-value" id="markets-count">0</div>
                            <div class="summary-label">Markets</div>
                        </div>
                    </div>

                    <section class="section">
                        <div class="section-header">
                            <span class="section-title">Active Markets</span>
                        </div>
                        <div class="section-body">
                            <div class="markets-grid" id="markets-grid">
                                <div class="empty-state">Waiting for markets...</div>
                            </div>
                        </div>
                    </section>
                </div>

                <!-- Analytics Tab -->
                <div class="tab-content" id="analytics-tab">
                    <div class="analytics-grid">
                        <div class="analytics-card">
                            <div class="analytics-title">Total Locked Profit</div>
                            <div class="analytics-value pos" id="a-locked">$0.00</div>
                            <div class="analytics-sub">Guaranteed profit from matched pairs</div>
                        </div>
                        <div class="analytics-card">
                            <div class="analytics-title">Total At Risk</div>
                            <div class="analytics-value neg" id="a-risk">$0.00</div>
                            <div class="analytics-sub">Unmatched position exposure</div>
                        </div>
                        <div class="analytics-card">
                            <div class="analytics-title">Risk/Reward Ratio</div>
                            <div class="analytics-value" id="a-ratio">0.00</div>
                            <div class="analytics-sub">Locked profit per $ at risk</div>
                        </div>
                        <div class="analytics-card">
                            <div class="analytics-title">Efficiency</div>
                            <div class="analytics-value" id="a-efficiency">0%</div>
                            <div class="analytics-sub">% of invested capital matched</div>
                        </div>

                        <div class="analytics-card wide">
                            <div class="analytics-title">Profit Over Time</div>
                            <div class="analytics-chart">
                                <canvas id="pnl-chart"></canvas>
                            </div>
                        </div>

                        <div class="analytics-card wide">
                            <div class="analytics-title">Trade Distribution</div>
                            <div class="analytics-chart">
                                <canvas id="trades-chart"></canvas>
                            </div>
                        </div>

                        <div class="analytics-card full">
                            <div class="analytics-title">Position Analysis</div>
                            <table class="stats-table" id="position-table">
                                <thead>
                                    <tr>
                                        <th>Asset</th>
                                        <th>YES Shares</th>
                                        <th>NO Shares</th>
                                        <th>Matched</th>
                                        <th>Avg YES</th>
                                        <th>Avg NO</th>
                                        <th>Combined Cost</th>
                                        <th>Locked Profit</th>
                                        <th>At Risk</th>
                                        <th>Status</th>
                                    </tr>
                                </thead>
                                <tbody id="position-tbody">
                                    <tr><td colspan="10" class="empty-state">No positions</td></tr>
                                </tbody>
                            </table>
                        </div>

                        <div class="analytics-card wide">
                            <div class="analytics-title">Strategy Performance</div>
                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 8px;">
                                <div>
                                    <div style="font-size: 11px; color: var(--dim); margin-bottom: 4px;">YES Buys</div>
                                    <div style="font-size: 20px; font-weight: 600; color: var(--green);" id="a-yes-buys">0</div>
                                    <div class="progress-bar"><div class="progress-fill green" id="yes-bar" style="width: 50%"></div></div>
                                </div>
                                <div>
                                    <div style="font-size: 11px; color: var(--dim); margin-bottom: 4px;">NO Buys</div>
                                    <div style="font-size: 20px; font-weight: 600; color: var(--red);" id="a-no-buys">0</div>
                                    <div class="progress-bar"><div class="progress-fill red" id="no-bar" style="width: 50%"></div></div>
                                </div>
                            </div>
                        </div>

                        <div class="analytics-card wide">
                            <div class="analytics-title">Recommendations</div>
                            <div id="recommendations" style="font-size: 12px; line-height: 1.6; color: var(--dim);">
                                Analyzing performance...
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <aside class="sidebar" id="sidebar">
                <div class="sidebar-section">
                    <div class="sidebar-title">Configuration</div>
                    <div class="config-row">
                        <span class="config-label">Trade Size</span>
                        <input type="number" class="config-input" id="trade-size" value="10" step="1">
                    </div>
                    <div class="config-row">
                        <span class="config-label">Dip Threshold</span>
                        <input type="number" class="config-input" id="dip-threshold" value="5" step="1">
                        <span style="color: var(--muted); font-size: 11px; margin-left: 4px;">%</span>
                    </div>
                </div>

                <div class="sidebar-section trades-section">
                    <div class="sidebar-title">Recent Trades</div>
                    <div class="trades-list" id="trades-list">
                        <div class="empty-state">No trades yet</div>
                    </div>
                </div>
            </aside>
        </div>
    </div>

    <script>
        const socket = io();
        let trades = [];
        let charts = {};
        let priceHistory = {};
        let pnlChart = null;
        let tradesChart = null;
        let startTime = Date.now();
        let pnlHistory = [];
        let yesBuys = 0, noBuys = 0;

        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(tab.dataset.tab + '-tab').classList.add('active');

                // Show/hide sidebar based on tab
                const sidebar = document.getElementById('sidebar');
                const main = document.getElementById('main-container');
                if (tab.dataset.tab === 'analytics') {
                    sidebar.style.display = 'none';
                    main.classList.add('full-width');
                } else {
                    sidebar.style.display = '';
                    main.classList.remove('full-width');
                }
            });
        });

        function updateClock() {
            document.getElementById('clock').textContent = new Date().toLocaleTimeString('en-US', {hour12: false});

            // Update runtime
            const elapsed = Math.floor((Date.now() - startTime) / 1000);
            const hrs = Math.floor(elapsed / 3600);
            const mins = Math.floor((elapsed % 3600) / 60);
            const secs = elapsed % 60;
            document.getElementById('runtime').textContent =
                `Runtime: ${hrs}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
        }
        setInterval(updateClock, 1000);
        updateClock();

        function fmt(v) { return (v >= 0 ? '+' : '') + '$' + v.toFixed(2); }
        function fmtPct(v) { return (v * 100).toFixed(1) + '%'; }
        function fmtTime(mins) {
            const m = Math.floor(mins);
            const s = Math.round((mins - m) * 60);
            return m + ':' + String(s).padStart(2, '0');
        }

        // Initialize analytics charts
        function initAnalyticsCharts() {
            const pnlCtx = document.getElementById('pnl-chart');
            if (pnlCtx && !pnlChart) {
                pnlChart = new Chart(pnlCtx, {
                    type: 'line',
                    data: {
                        labels: [],
                        datasets: [{
                            label: 'Locked Profit',
                            data: [],
                            borderColor: '#22c55e',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            pointRadius: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } },
                        scales: {
                            x: { display: false },
                            y: {
                                grid: { color: '#222' },
                                ticks: { color: '#555', callback: v => '$' + v }
                            }
                        },
                        animation: false
                    }
                });
            }

            const tradesCtx = document.getElementById('trades-chart');
            if (tradesCtx && !tradesChart) {
                tradesChart = new Chart(tradesCtx, {
                    type: 'doughnut',
                    data: {
                        labels: ['YES Buys', 'NO Buys'],
                        datasets: [{
                            data: [50, 50],
                            backgroundColor: ['rgba(34, 197, 94, 0.8)', 'rgba(239, 68, 68, 0.8)'],
                            borderWidth: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                position: 'bottom',
                                labels: { color: '#888', font: { size: 11 } }
                            }
                        },
                        animation: false
                    }
                });
            }
        }

        setTimeout(initAnalyticsCharts, 500);

        socket.on('connect', () => {
            document.getElementById('status-dot').classList.add('live');
            document.getElementById('status-text').textContent = 'live';
        });

        socket.on('disconnect', () => {
            document.getElementById('status-dot').classList.remove('live');
            document.getElementById('status-text').textContent = 'disconnected';
        });

        function createChart(canvasId, marketId) {
            const ctx = document.getElementById(canvasId);
            if (!ctx) return null;

            if (!priceHistory[marketId]) {
                priceHistory[marketId] = { labels: [], yes: [], no: [] };
            }

            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: priceHistory[marketId].labels,
                    datasets: [
                        {
                            label: 'YES',
                            data: priceHistory[marketId].yes,
                            borderColor: '#22c55e',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            pointRadius: 0,
                        },
                        {
                            label: 'NO',
                            data: priceHistory[marketId].no,
                            borderColor: '#ef4444',
                            backgroundColor: 'rgba(239, 68, 68, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            pointRadius: 0,
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { intersect: false, mode: 'index' },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: '#1a1a1a',
                            titleColor: '#888',
                            bodyColor: '#e8e8e8',
                            borderColor: '#333',
                            borderWidth: 1,
                            callbacks: {
                                label: ctx => ctx.dataset.label + ': ' + (ctx.raw * 100).toFixed(1) + '%'
                            }
                        }
                    },
                    scales: {
                        x: { display: false },
                        y: {
                            display: true,
                            min: 0,
                            max: 1,
                            grid: { color: '#222' },
                            ticks: {
                                color: '#555',
                                font: { size: 10 },
                                callback: v => (v * 100) + '%',
                                stepSize: 0.25
                            }
                        }
                    },
                    animation: false
                }
            });

            charts[marketId] = chart;
            return chart;
        }

        function updateChart(marketId, yesPrice, noPrice) {
            if (!priceHistory[marketId]) {
                priceHistory[marketId] = { labels: [], yes: [], no: [] };
            }

            const now = new Date().toLocaleTimeString('en-US', {hour12: false});
            priceHistory[marketId].labels.push(now);
            priceHistory[marketId].yes.push(yesPrice);
            priceHistory[marketId].no.push(noPrice);

            if (priceHistory[marketId].labels.length > 120) {
                priceHistory[marketId].labels.shift();
                priceHistory[marketId].yes.shift();
                priceHistory[marketId].no.shift();
            }

            if (charts[marketId]) {
                charts[marketId].data.labels = priceHistory[marketId].labels;
                charts[marketId].data.datasets[0].data = priceHistory[marketId].yes;
                charts[marketId].data.datasets[1].data = priceHistory[marketId].no;
                charts[marketId].update('none');
            }
        }

        function updateAnalytics(data) {
            const locked = data.total_locked_profit || 0;
            const risk = data.total_exposure || 0;
            const positions = data.positions || {};

            document.getElementById('a-locked').textContent = fmt(locked);
            document.getElementById('a-risk').textContent = '$' + risk.toFixed(2);

            const ratio = risk > 0 ? (locked / risk).toFixed(2) : '∞';
            document.getElementById('a-ratio').textContent = ratio;

            let totalInvested = 0;
            let totalMatched = 0;
            Object.values(positions).forEach(p => {
                totalInvested += (p.cost_yes || 0) + (p.cost_no || 0);
                totalMatched += (p.matched_shares || 0) * 2;
            });

            const efficiency = totalInvested > 0 ? ((totalMatched / totalInvested) * 100).toFixed(0) : 0;
            document.getElementById('a-efficiency').textContent = efficiency + '%';

            // Update PnL history chart
            pnlHistory.push({ time: new Date().toLocaleTimeString('en-US', {hour12: false}), value: locked });
            if (pnlHistory.length > 200) pnlHistory.shift();

            if (pnlChart) {
                pnlChart.data.labels = pnlHistory.map(p => p.time);
                pnlChart.data.datasets[0].data = pnlHistory.map(p => p.value);
                pnlChart.update('none');
            }

            // Update trades chart
            if (tradesChart) {
                tradesChart.data.datasets[0].data = [yesBuys, noBuys];
                tradesChart.update('none');
            }

            document.getElementById('a-yes-buys').textContent = yesBuys;
            document.getElementById('a-no-buys').textContent = noBuys;
            const totalBuys = yesBuys + noBuys;
            if (totalBuys > 0) {
                document.getElementById('yes-bar').style.width = (yesBuys / totalBuys * 100) + '%';
                document.getElementById('no-bar').style.width = (noBuys / totalBuys * 100) + '%';
            }

            // Update position table
            const tbody = document.getElementById('position-tbody');
            const posEntries = Object.entries(positions).filter(([_, p]) => p.shares_yes > 0 || p.shares_no > 0);

            if (posEntries.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No positions</td></tr>';
            } else {
                tbody.innerHTML = posEntries.map(([cid, p]) => {
                    const markets = data.markets || {};
                    const market = markets[cid];
                    const asset = market ? market.asset : cid.substring(0, 8);
                    const combinedCost = (p.avg_price_yes || 0) + (p.avg_price_no || 0);
                    const profitable = combinedCost < 1;
                    const status = p.matched_shares > 0 ?
                        (profitable ? '<span style="color: var(--green)">✓ Profitable</span>' : '<span style="color: var(--red)">⚠ Loss</span>') :
                        '<span style="color: var(--dim)">Building</span>';

                    return `
                        <tr>
                            <td><strong>${asset}</strong></td>
                            <td>${p.shares_yes.toFixed(1)}</td>
                            <td>${p.shares_no.toFixed(1)}</td>
                            <td>${p.matched_shares.toFixed(1)}</td>
                            <td>${fmtPct(p.avg_price_yes)}</td>
                            <td>${fmtPct(p.avg_price_no)}</td>
                            <td style="color: ${combinedCost < 1 ? 'var(--green)' : 'var(--red)'}">${fmtPct(combinedCost)}</td>
                            <td style="color: var(--green)">${fmt(p.locked_profit)}</td>
                            <td style="color: var(--red)">$${p.total_exposure.toFixed(2)}</td>
                            <td>${status}</td>
                        </tr>
                    `;
                }).join('');
            }

            // Generate recommendations
            const recs = [];
            if (locked > 0) recs.push(`✓ Strategy is generating locked profit: ${fmt(locked)}`);
            if (risk > locked * 2) recs.push(`⚠ High risk exposure (${fmt(risk)}) relative to locked profit. Consider reducing position sizes.`);
            if (efficiency < 30) recs.push(`⚠ Low matching efficiency (${efficiency}%). May need to adjust dip threshold.`);
            if (yesBuys > noBuys * 2) recs.push(`ℹ Buying more YES than NO. Markets may be biased - consider the trend.`);
            if (noBuys > yesBuys * 2) recs.push(`ℹ Buying more NO than YES. Markets may be biased - consider the trend.`);
            if (locked > 10) recs.push(`✓ Strong performance! Consider increasing trade size gradually.`);
            if (recs.length === 0) recs.push('Collecting data for analysis...');

            document.getElementById('recommendations').innerHTML = recs.map(r => `<div style="margin-bottom: 8px;">${r}</div>`).join('');
        }

        socket.on('state_update', (data) => {
            const pnl = data.total_pnl || 0;
            const pnlEl = document.getElementById('total-pnl');
            pnlEl.textContent = fmt(pnl);
            pnlEl.className = 'metric-val ' + (pnl >= 0 ? 'pos' : 'neg');

            const locked = data.total_locked_profit || 0;
            const lockedEl = document.getElementById('locked-profit');
            lockedEl.textContent = fmt(locked);
            lockedEl.className = 'metric-val ' + (locked > 0 ? 'pos' : '');

            document.getElementById('trade-count').textContent = data.trade_count || 0;

            let totalInvested = 0;
            let totalMatched = 0;
            let totalShares = 0;

            const positions = data.positions || {};
            Object.values(positions).forEach(p => {
                totalInvested += (p.cost_yes || 0) + (p.cost_no || 0);
                totalMatched += p.matched_shares || 0;
                totalShares += (p.shares_yes || 0) + (p.shares_no || 0);
            });

            document.getElementById('capital').textContent = '$' + (data.capital || 1000).toLocaleString();
            document.getElementById('total-invested').textContent = '$' + totalInvested.toFixed(0);

            const totalLockedEl = document.getElementById('total-locked');
            totalLockedEl.textContent = fmt(locked);
            totalLockedEl.className = 'summary-value ' + (locked > 0 ? 'pos' : '');

            const exposureEl = document.getElementById('total-exposure');
            exposureEl.textContent = '$' + (data.total_exposure || 0).toFixed(2);
            exposureEl.className = 'summary-value ' + ((data.total_exposure || 0) > 0 ? 'neg' : '');

            const matchedPct = totalShares > 0 ? (totalMatched * 2 / totalShares * 100) : 0;
            document.getElementById('matched-pct').textContent = matchedPct.toFixed(0) + '%';

            document.getElementById('markets-count').textContent = Object.keys(data.markets || {}).length;

            const markets = data.markets || {};
            const grid = document.getElementById('markets-grid');

            if (Object.keys(markets).length === 0) {
                grid.innerHTML = '<div class="empty-state">Waiting for markets...</div>';
            } else {
                const currentIds = Object.keys(markets).sort().join(',');
                const existingIds = Array.from(grid.querySelectorAll('.market-card')).map(el => el.dataset.cid).sort().join(',');

                if (currentIds !== existingIds) {
                    grid.innerHTML = Object.entries(markets).map(([cid, m]) => {
                        const shortId = cid.substring(0, 8);
                        return `
                            <div class="market-card" data-cid="${cid}">
                                <div class="market-header">
                                    <div class="market-title">
                                        <span class="market-asset">${m.asset}</span>
                                        <span class="market-time">${fmtTime(m.time_left)}</span>
                                    </div>
                                    <div class="market-prices">
                                        <div class="price-badge yes"><span class="label">YES</span><span class="value">${fmtPct(m.yes_price)}</span></div>
                                        <div class="price-badge no"><span class="label">NO</span><span class="value">${fmtPct(m.no_price)}</span></div>
                                    </div>
                                </div>
                                <div class="chart-container"><canvas id="chart-${shortId}"></canvas></div>
                                <div class="market-position" id="pos-${shortId}"><div class="no-position">No position</div></div>
                            </div>
                        `;
                    }).join('');

                    Object.entries(markets).forEach(([cid, m]) => {
                        const shortId = cid.substring(0, 8);
                        if (!charts[cid]) {
                            setTimeout(() => createChart('chart-' + shortId, cid), 100);
                        }
                    });
                }

                Object.entries(markets).forEach(([cid, m]) => {
                    const shortId = cid.substring(0, 8);
                    const card = grid.querySelector(`[data-cid="${cid}"]`);
                    if (!card) return;

                    const pos = positions[cid];
                    const hasPos = pos && (pos.shares_yes > 0 || pos.shares_no > 0);

                    card.className = 'market-card' + (hasPos ? ' has-position' : '');

                    const timeEl = card.querySelector('.market-time');
                    timeEl.textContent = fmtTime(m.time_left);
                    timeEl.className = 'market-time' + (m.time_left < 2 ? ' urgent' : '');

                    card.querySelector('.price-badge.yes .value').textContent = fmtPct(m.yes_price);
                    card.querySelector('.price-badge.no .value').textContent = fmtPct(m.no_price);

                    updateChart(cid, m.yes_price, m.no_price);

                    const posEl = document.getElementById('pos-' + shortId);
                    if (posEl) {
                        if (hasPos) {
                            posEl.innerHTML = `
                                <div class="position-grid">
                                    <div class="position-item"><div class="position-value">${pos.shares_yes.toFixed(1)}</div><div class="position-label">YES</div></div>
                                    <div class="position-item"><div class="position-value">${pos.shares_no.toFixed(1)}</div><div class="position-label">NO</div></div>
                                    <div class="position-item"><div class="position-value">${pos.matched_shares.toFixed(1)}</div><div class="position-label">Matched</div></div>
                                    <div class="position-item"><div class="position-value ${pos.locked_profit >= 0 ? 'pos' : 'neg'}">${fmt(pos.locked_profit)}</div><div class="position-label">Locked</div></div>
                                    <div class="position-item"><div class="position-value ${pos.total_exposure > 0 ? 'neg' : ''}">$${pos.total_exposure.toFixed(2)}</div><div class="position-label">At Risk</div></div>
                                </div>
                            `;
                        } else {
                            posEl.innerHTML = '<div class="no-position">No position</div>';
                        }
                    }
                });
            }

            updateAnalytics(data);
        });

        socket.on('trade', (trade) => {
            trades.unshift(trade);
            if (trades.length > 30) trades.pop();

            // Track YES/NO buys
            if (trade.side === 'YES') yesBuys++;
            else if (trade.side === 'NO') noBuys++;

            const list = document.getElementById('trades-list');
            list.innerHTML = trades.map(t => {
                const isYes = t.side === 'YES';
                return `
                    <div class="trade-item ${isYes ? 'yes' : 'no'}">
                        <span class="trade-side ${isYes ? 'yes' : 'no'}">${t.side}</span>
                        <div class="trade-info">
                            <div class="trade-asset">${t.asset}</div>
                            <div class="trade-time">${t.time || 'now'}</div>
                        </div>
                        <div class="trade-details">
                            <div class="trade-price">${fmtPct(t.price)}</div>
                            <div class="trade-shares">${t.shares.toFixed(1)} @ $${t.cost.toFixed(2)}</div>
                        </div>
                    </div>
                `;
            }).join('');
        });

        document.getElementById('trade-size').addEventListener('change', (e) => {
            socket.emit('config_update', { trade_size: parseFloat(e.target.value) });
        });
        document.getElementById('dip-threshold').addEventListener('change', (e) => {
            socket.emit('config_update', { dip_threshold: parseFloat(e.target.value) / 100 });
        });
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/state')
def get_state():
    return jsonify({
        'capital': dashboard_state.capital,
        'total_pnl': dashboard_state.total_pnl,
        'trade_count': dashboard_state.trade_count,
        'total_locked_profit': dashboard_state.total_locked_profit,
        'total_exposure': dashboard_state.total_exposure,
        'markets': dashboard_state.markets,
        'positions': dashboard_state.positions,
    })


@app.route('/api/trades')
def get_trades():
    """Get trade history from database."""
    try:
        db_path = Path('data/gabagool.db')
        if not db_path.exists():
            return jsonify([])

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT timestamp, market_id, asset, side, shares, price, cost, ma, dip_pct
            FROM trades ORDER BY timestamp DESC LIMIT 100
        """)
        rows = cursor.fetchall()
        conn.close()

        return jsonify([{
            'timestamp': r[0],
            'market_id': r[1],
            'asset': r[2],
            'side': r[3],
            'shares': r[4],
            'price': r[5],
            'cost': r[6],
            'ma': r[7],
            'dip_pct': r[8],
        } for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/stats')
def get_stats():
    """Get overall stats from database."""
    try:
        db_path = Path('data/gabagool.db')
        if not db_path.exists():
            return jsonify({})

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stats WHERE id = 1")
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({
                'starting_capital': row[1],
                'current_capital': row[2],
                'total_pnl': row[3],
                'total_trades': row[4],
                'total_yes_buys': row[5],
                'total_no_buys': row[6],
                'markets_completed': row[7],
                'started_at': row[8],
                'updated_at': row[9],
            })
        return jsonify({})
    except Exception as e:
        return jsonify({'error': str(e)})


def emit_state():
    socketio.emit('state_update', {
        'capital': dashboard_state.capital,
        'total_pnl': dashboard_state.total_pnl,
        'trade_count': dashboard_state.trade_count,
        'markets_completed': dashboard_state.markets_completed,
        'total_locked_profit': dashboard_state.total_locked_profit,
        'total_exposure': dashboard_state.total_exposure,
        'markets': dashboard_state.markets,
        'positions': dashboard_state.positions,
    })


def emit_trade(trade: dict):
    trade['time'] = datetime.now().strftime('%H:%M:%S')
    dashboard_state.trades.insert(0, trade)
    if len(dashboard_state.trades) > 50:
        dashboard_state.trades.pop()
    socketio.emit('trade', trade)


@socketio.on('config_update')
def handle_config_update(data):
    if 'trade_size' in data:
        dashboard_state.config['trade_size'] = data['trade_size']
    if 'dip_threshold' in data:
        dashboard_state.config['dip_threshold'] = data['dip_threshold']
    print(f"Config updated: {data}")


def update_dashboard_state(state: dict):
    dashboard_state.capital = state.get('capital', 1000)
    dashboard_state.total_pnl = state.get('total_pnl', 0)
    dashboard_state.trade_count = state.get('trade_count', 0)
    dashboard_state.markets_completed = state.get('markets_completed', 0)
    dashboard_state.total_locked_profit = state.get('total_locked_profit', 0)
    dashboard_state.total_exposure = state.get('total_exposure', 0)
    dashboard_state.markets = state.get('markets', {})
    dashboard_state.positions = state.get('positions', {})
    dashboard_state.last_update = datetime.now(timezone.utc)


def state_emitter():
    while True:
        time.sleep(0.5)
        emit_state()


def run_dashboard(host='0.0.0.0', port=5050):
    import os
    port = int(os.environ.get('PORT', port))

    print(f"\n  GABAGOOL Dashboard")
    print(f"  http://localhost:{port}\n")

    emitter_thread = threading.Thread(target=state_emitter, daemon=True)
    emitter_thread.start()

    socketio.run(app, host=host, port=port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="Gabagool Dashboard")
    parser.add_argument('--port', type=int, default=5050, help='Dashboard port')
    parser.add_argument('--with-engine', action='store_true', help='Start with trading engine')
    args = parser.parse_args()

    if args.with_engine:
        print("Starting dashboard with trading engine...")
        import asyncio
        from engine import GabagoolEngine, EngineConfig

        config = EngineConfig()
        engine = GabagoolEngine(
            config=config,
            on_state_update=update_dashboard_state,
            on_trade=emit_trade,
        )

        def run_engine():
            asyncio.run(engine.run())

        engine_thread = threading.Thread(target=run_engine, daemon=True)
        engine_thread.start()

    run_dashboard(port=args.port)
