# PolyBot — Polymarket Copy-Trading System

A complete, production-ready automated copy-trading system for [Polymarket](https://polymarket.com) built on Polygon.

## 🗂️ Project Structure

```
PolyBot/
├── src/
│   ├── wallet_discovery.py   # Module 1 — Scan & rank top directional traders
│   ├── risk_management.py    # Module 2 — Kelly Criterion position sizing
│   ├── trade_execution.py    # Module 3 — Real-time trade monitoring & execution
│   ├── paper_trading.py      # Module 4 — Paper trading simulator
│   └── utils.py              # Shared helpers (logging, JSON, env)
├── backend/
│   └── main.py               # Module 5 — FastAPI dashboard backend + WebSocket
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   └── components/
│   │       ├── Dashboard.jsx
│   │       ├── Positions.jsx
│   │       └── Alerts.jsx
│   ├── package.json
│   └── vite.config.js
├── tests/
│   ├── test_wallet_discovery.py
│   ├── test_risk_management.py
│   └── test_paper_trading.py
├── scripts/
│   ├── deploy.sh             # Deployment script
│   └── nginx.conf            # Nginx reverse proxy config
├── docker-compose.yml        # Orchestrate all services
├── Dockerfile                # Backend image
├── Dockerfile.frontend       # Frontend image (nginx)
├── requirements.txt
└── .env.example
```

## 🧩 Modules

### 1. Wallet Discovery (`src/wallet_discovery.py`)
Scans Polymarket wallets on Polygon and identifies the **top 10 directional traders** for copy trading.

**Filters:**
- ≥ 100 historical trades
- Win rate ≥ 60%
- Excludes leaderboard / top-visible wallets

**Computes per wallet:**
- Average win rate
- Average position size (USDC)
- Market focus (crypto, politics, sports, finance)
- Historical Sharpe ratio

**Output:** JSON list ready to feed into the Risk Management module.

```python
import asyncio
from src.wallet_discovery import discover_top_traders

traders = asyncio.run(discover_top_traders(top_n=10))
print(traders)
```

---

### 2. Risk Management (`src/risk_management.py`)
Calculates optimal USDC position sizes using the **Kelly Criterion**.

**Features:**
- Full / half / quarter Kelly modes
- Max 5% portfolio risk per trade
- Automatic slippage adjustment (1–3%)
- Modular — callable from any other module

```python
from src.risk_management import calculate_position_sizes

sizes = calculate_position_sizes(
    wallet_stats=traders,
    portfolio_value_usdc=50_000,
    kelly_mode="half",           # "full" | "half" | "quarter"
)
```

---

### 3. Trade Execution (`src/trade_execution.py`)
Real-time async monitoring and execution engine.

**Features:**
- Polls watched wallets every 500 ms (< 1 s detection latency)
- Order book depth check before execution
- Expected Value (EV) computation
- Automatic retry with exponential back-off
- `DRY_RUN=true` mode for safe testing
- Structured JSON trade log

```python
import asyncio
from src.trade_execution import monitor_wallets

asyncio.run(monitor_wallets(traders, portfolio_value_usdc=50_000))
```

---

### 4. Paper Trading Simulator (`src/paper_trading.py`)
Simulates copy trades without using real funds.

**Tracks:**
- Realised & unrealised PnL
- Expected EV per trade
- Max drawdown
- Win rate & Sharpe ratio
- ASCII sparkline equity curve

```python
from src.paper_trading import PaperTradingSimulator

sim = PaperTradingSimulator(starting_balance=10_000)
sim.record_trade("t1", "0xABC", "Will BTC hit $100k?", "BUY", 0.65, 500.0)
sim.close_trade("t1", 0.90)
report = sim.daily_summary()
```

---

### 5. Web Dashboard (`backend/` + `frontend/`)
Full-stack real-time dashboard.

**Backend (FastAPI):**
- `GET  /api/traders`     — top traders
- `GET  /api/positions`   — open positions
- `GET  /api/pnl`         — PnL summary
- `GET  /api/sizing`      — position sizing
- `GET  /api/report`      — daily summary report
- `POST /api/trades/open` — open a paper trade
- `POST /api/trades/close`— close a paper trade
- `GET  /api/alerts`      — trade log
- `WS   /ws`              — real-time WebSocket stream

**Frontend (React + Tailwind):**
- Dashboard with metric cards + equity chart
- Positions table with manual close
- Trade alerts log
- WebSocket live updates
- Telegram alert integration

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- Node.js 20+
- Docker & Docker Compose (for containerised deployment)

### Local Development

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your API keys

# 2. Backend
pip install -r requirements.txt
uvicorn backend.main:app --reload

# 3. Frontend (separate terminal)
cd frontend
npm install
npm run dev
# → http://localhost:3000
```

### Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

All 64 tests should pass.

---

## 🐳 Docker Deployment

```bash
# 1. Configure secrets
cp .env.example .env
# Edit .env

# 2. Deploy
bash scripts/deploy.sh

# Dashboard:  http://localhost:80
# API:        http://localhost:8000
# API docs:   http://localhost:8000/docs
```

---

## ⚙️ Configuration

All settings are controlled via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_CLOB_API_KEY` | — | Polymarket CLOB API key |
| `MIN_TRADES` | 100 | Minimum trades to qualify a wallet |
| `MIN_WIN_RATE` | 0.60 | Minimum win rate |
| `TOP_N` | 10 | Number of top traders to output |
| `KELLY_MODE` | half | Kelly fraction: full / half / quarter |
| `DRY_RUN` | true | Paper trade only (no real orders) |
| `POLL_INTERVAL_MS` | 500 | Trade detection polling interval |
| `PORTFOLIO_VALUE_USDC` | 50000 | Total portfolio for sizing |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot for alerts |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID |

---

## 🛡️ Security Notes

- **Never commit `.env`** — it is in `.gitignore`
- The `DRY_RUN` flag is `true` by default — set it to `false` only after thorough testing
- API keys are loaded from environment variables only
- Docker containers run as non-root users
- This system is for educational/research purposes — trading involves financial risk

---

## 📄 License

MIT
