"""
PolyBot Dashboard Backend
=========================
FastAPI application providing:
  - REST endpoints for wallet stats, positions, PnL, and risk metrics
  - WebSocket endpoint for real-time trade stream updates
  - Telegram alert integration for executed trades and high-risk exposure
  - Integration with Wallet Discovery, Risk Management, and Paper Trading modules

Run:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.paper_trading import PaperTradingSimulator
from src.risk_management import calculate_position_sizes
from src.wallet_discovery import discover_top_traders

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORTFOLIO_VALUE_USDC = float(os.getenv("PORTFOLIO_VALUE_USDC", "50000"))
KELLY_MODE = os.getenv("KELLY_MODE", "half")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")

# ---------------------------------------------------------------------------
# In-memory state (replace with Redis / DB in production)
# ---------------------------------------------------------------------------
_state: dict[str, Any] = {
    "top_traders": [],
    "position_sizes": [],
    "simulator": PaperTradingSimulator(starting_balance=PORTFOLIO_VALUE_USDC),
    "trade_log": [],
    "last_discovery": 0.0,
}

# Active WebSocket connections
_ws_clients: set[WebSocket] = set()


# ---------------------------------------------------------------------------
# Lifespan: start background tasks on startup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Run background discovery + broadcast loop while app is live."""
    task = asyncio.create_task(_background_discovery_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="PolyBot Dashboard API",
    description="Real-time Polymarket copy-trading system dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def _background_discovery_loop(interval_seconds: int = 300) -> None:
    """Refresh top traders and position sizes every *interval_seconds*."""
    while True:
        try:
            logger.info("Running wallet discovery …")
            traders = await discover_top_traders()
            sizes = calculate_position_sizes(traders, PORTFOLIO_VALUE_USDC, KELLY_MODE)
            _state["top_traders"] = traders
            _state["position_sizes"] = sizes
            _state["last_discovery"] = time.time()
            await _broadcast({"type": "discovery_update", "count": len(traders)})
        except Exception as exc:  # noqa: BLE001
            logger.error("Background discovery error: %s", exc)
        await asyncio.sleep(interval_seconds)


async def _broadcast(message: dict) -> None:
    """Send a JSON message to all connected WebSocket clients."""
    dead: set[WebSocket] = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:  # noqa: BLE001
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------
async def send_telegram_alert(text: str) -> None:
    """Send a Telegram message to the configured chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram alert failed: %s", exc)


# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "timestamp": time.time()}


@app.get("/api/traders")
async def get_top_traders() -> dict:
    """Return the latest discovered top traders."""
    return {
        "traders": _state["top_traders"],
        "last_updated": _state["last_discovery"],
        "count": len(_state["top_traders"]),
    }


@app.get("/api/positions")
async def get_positions() -> dict:
    """Return current active (paper) positions."""
    sim: PaperTradingSimulator = _state["simulator"]
    return {
        "open_positions": [
            {
                "trade_id": t.trade_id,
                "wallet": t.wallet,
                "market": t.market,
                "side": t.side,
                "entry_price": t.entry_price,
                "size_usdc": t.size_usdc,
                "expected_ev_usdc": t.expected_ev_usdc,
                "timestamp": t.timestamp,
            }
            for t in sim.open_trades()
        ],
        "count": len(sim.open_trades()),
    }


@app.get("/api/pnl")
async def get_pnl() -> dict:
    """Return realised and unrealised PnL summary."""
    sim: PaperTradingSimulator = _state["simulator"]
    return {
        "realised_pnl_usdc": sim.total_realised_pnl(),
        "unrealised_pnl_usdc": sim.unrealised_pnl(),
        "win_rate": sim.win_rate(),
        "max_drawdown_pct": round(sim.max_drawdown() * 100, 2),
        "sharpe_ratio": sim.sharpe_ratio(),
        "total_trades": len(sim.trades),
    }


@app.get("/api/sizing")
async def get_position_sizes() -> dict:
    """Return recommended position sizes from the Risk Management module."""
    return {
        "sizing": _state["position_sizes"],
        "portfolio_value_usdc": PORTFOLIO_VALUE_USDC,
        "kelly_mode": KELLY_MODE,
    }


@app.get("/api/report")
async def get_daily_report() -> dict:
    """Return the full daily summary report from the paper trading simulator."""
    sim: PaperTradingSimulator = _state["simulator"]
    return sim.daily_summary()


@app.post("/api/trades/open")
async def open_paper_trade(trade: dict) -> dict:
    """
    Open a new paper trade.

    Expected body::

        {
          "trade_id": "...",
          "wallet": "0x...",
          "market": "Will BTC hit $100k?",
          "side": "BUY",
          "entry_price": 0.65,
          "size_usdc": 500,
          "expected_ev_usdc": 12.5
        }
    """
    sim: PaperTradingSimulator = _state["simulator"]
    result = sim.record_trade(
        trade_id=trade.get("trade_id", str(time.time())),
        wallet=trade.get("wallet", ""),
        market=trade.get("market", ""),
        side=trade.get("side", "BUY"),
        nominal_price=float(trade.get("entry_price", 0)),
        size_usdc=float(trade.get("size_usdc", 0)),
        expected_ev_usdc=float(trade.get("expected_ev_usdc", 0)),
    )
    if result is None:
        raise HTTPException(status_code=400, detail="Insufficient balance or invalid trade")

    log_entry = {
        "trade_id": result.trade_id,
        "wallet": result.wallet,
        "market": result.market,
        "status": "opened",
        "timestamp": result.timestamp,
    }
    _state["trade_log"].append(log_entry)
    await _broadcast({"type": "trade_opened", "trade": log_entry})
    await send_telegram_alert(
        f"✅ Paper trade opened\n"
        f"Market: {result.market}\n"
        f"Side: {result.side}  Size: ${result.size_usdc:.2f}  Price: {result.entry_price:.4f}"
    )
    return log_entry


@app.post("/api/trades/close")
async def close_paper_trade(payload: dict) -> dict:
    """
    Close an existing paper trade.

    Expected body::

        {"trade_id": "...", "exit_price": 0.90}
    """
    sim: PaperTradingSimulator = _state["simulator"]
    trade_id = payload.get("trade_id", "")
    exit_price = float(payload.get("exit_price", 0))
    result = sim.close_trade(trade_id, exit_price)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    log_entry = {
        "trade_id": result.trade_id,
        "outcome": result.outcome,
        "realised_pnl_usdc": result.realised_pnl,
        "exit_price": result.exit_price,
    }
    await _broadcast({"type": "trade_closed", "trade": log_entry})
    await send_telegram_alert(
        f"{'🟢' if result.outcome == 'win' else '🔴'} Paper trade closed\n"
        f"Outcome: {result.outcome.upper()}  PnL: ${result.realised_pnl:.2f}"
    )
    return log_entry


@app.get("/api/alerts")
async def get_trade_log() -> dict:
    """Return the in-memory trade log."""
    return {"log": _state["trade_log"][-100:]}


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """
    WebSocket endpoint for real-time trade stream.

    Pushes updates to all connected clients whenever:
      - A new wallet discovery cycle completes
      - A paper trade is opened or closed
    """
    await ws.accept()
    _ws_clients.add(ws)
    logger.info("WebSocket client connected (%d total)", len(_ws_clients))
    try:
        # Send current state on connect
        await ws.send_text(json.dumps({
            "type": "init",
            "traders_count": len(_state["top_traders"]),
            "open_positions": len(_state["simulator"].open_trades()),
        }))
        # Keep connection alive until client disconnects
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(_ws_clients))
