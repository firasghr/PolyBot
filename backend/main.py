"""
PolyBot Dashboard Backend
=========================
FastAPI application providing:
  - REST endpoints for wallet stats, positions, PnL, and risk metrics
  - WebSocket endpoint for real-time trade stream updates
  - Telegram alert integration for executed trades and high-risk exposure
  - Integration with Wallet Discovery, Risk Management, Trade Detection, and Paper Trading modules

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

from src.market_cache import MarketCache
from src.paper_trading import PaperTradingSimulator
from src.risk_management import calculate_position_sizes, PortfolioRiskManager
from src.trade_execution import TradeDetectionService
from src.basket_strategy import BasketStrategy
from src.wallet_discovery import discover_top_traders, _classify_market

from database.db import init_db, async_session_maker
from database.repository import DBRepository
from database.redis_client import redis_db

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
BASKET_TIME_WINDOW_S = int(os.getenv("BASKET_TIME_WINDOW_S", "300"))
MODE = os.getenv("MODE", "paper")

# ---------------------------------------------------------------------------
# In-memory state (replace with Redis / DB in production)
# ---------------------------------------------------------------------------
market_cache = MarketCache()
basket_strategy = BasketStrategy(time_window_s=BASKET_TIME_WINDOW_S)
risk_manager = PortfolioRiskManager()

_state: dict[str, Any] = {
    "top_traders": [],
    "position_sizes": [],
    "sizing_map": {},  # wallet -> recommended USDC size
    "trade_log": [],
    "last_discovery": 0.0,
}

# Active WebSocket connections
_ws_clients: set[WebSocket] = set()


# ---------------------------------------------------------------------------
# Signal Pipeline
# ---------------------------------------------------------------------------
async def _on_trade_detected(signal: dict[str, Any]) -> None:
    """
    Callback fired by TradeDetectionService when a watched wallet enters a trade.
    Pipes the signal through BasketStrategy, applies RiskManagement sizing,
    executes the PaperTrade, and broadcasts the event.
    """
    wallet = signal["wallet"]
    condition_id = signal["condition_id"]
    outcome = signal["outcome"]
    price = signal["entry_price"]
    
    if not condition_id or not outcome:
        return

    signal_type = signal.get("signal_type", "ENTRY")

    # 1. Look up base recommended position size for this wallet
    base_size = _state["sizing_map"].get(wallet, 0.0)
    if base_size <= 0:
        return

    sim: PaperTradingSimulator = _state["simulator"]
    market_str = f"[{condition_id[:8]}] {signal['title'] or 'Unknown'} - {outcome}"

    # Handle EXIT Signals (SELL orders)
    if signal_type == "EXIT":
        logger.info("EXIT signal received for %s on %s", wallet, market_str)
        
        async with async_session_maker() as session:
            repo = DBRepository(session)
            sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
            # Find if we have an open position matching this market to close
            open_trades = [t for t in await sim.open_trades() if t.market_title == market_str]
            for t in open_trades:
                await sim.close_trade(t.id, exit_price=price)
                logger.info("Closed paper trade %s due to wallet EXIT signal at %.4f", t.id, price)
        return

    # Handle ENTRY Signals (BUY orders)
    
    # 2. Check basket strategy for multi-wallet confluence
    basket_strategy.record_signal(wallet, condition_id, outcome, signal["timestamp"])
    confluence = basket_strategy.check_confluence(condition_id, outcome)
    adjusted_size = base_size * confluence.multiplier

    # 3. Check Portfolio Risk Manager (Category & Count Limits)
    category = _classify_market(signal.get("title", ""))
    
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        
        can_trade, reason = risk_manager.can_open_trade(
            new_trade_size=adjusted_size,
            category=category,
            total_portfolio_value=sim.balance,
            open_trades=await sim.open_trades(),
        )
        if not can_trade:
            logger.info("Trade rejected by Risk Manager: %s", reason)
            return

        # 4. Fetch Orderbook & Calculate Exact Slippage
        orderbook = await market_cache.get_orderbook(condition_id)
        
        logger.debug("Applying exact orderbook slippage check for %s", condition_id)
        from src.risk_management import calculate_exact_slippage
        effective_size, slippage_pct, abort_reason = calculate_exact_slippage(
            desired_size_usdc=adjusted_size, 
            orderbook=orderbook, 
            side=signal["side"]
        )
        
        if abort_reason:
            logger.warning("Trade aborted due to slippage rules: %s", abort_reason)
            return

        # 5. Feed to paper trading simulator (or EVM in Live Mode)
        if MODE == "paper":
            trade = await sim.record_trade(
                trade_id=signal["signal_id"],
                wallet=wallet,
                market=market_str,
                side=signal["side"],
                nominal_price=price,
                size_usdc=effective_size,
                category=category,
            )
            
            if trade:
                log_entry = {
                    "trade_id": trade.id,
                    "wallet": wallet,
                    "market": market_str,
                    "status": "opened",
                    "timestamp": trade.entry_timestamp,
                }
                _state["trade_log"].append(log_entry)
                await _broadcast({"type": "trade_opened", "trade": log_entry})
                
                msg = (
                    f"✅ Paper trade opened\n"
                    f"Wallet: {wallet[:6]}…{wallet[-4:]}\n"
                    f"Market: {market_str}\n"
                    f"Size: ${effective_size:.2f}  Price: {price:.4f}  Slippage: {slippage_pct*100:.2f}%"
                )
                if confluence.multiplier > 1.0:
                    msg += f"\n🔥 Confluence Multiplier: {confluence.multiplier}x"
                
                await send_telegram_alert(msg)
        elif MODE == "live":
            from src.evm_execution import EVMExecutionService
            live_executable = EVMExecutionService(repo)
            trade = await live_executable.execute_trade(
                trade_id=signal["signal_id"],
                wallet=wallet,
                market=market_str,
                side=signal["side"],
                size_usdc=effective_size,
                nominal_price=price,
                category=category,
            )
            
            if trade:
                log_entry = {
                    "trade_id": trade.id,
                    "wallet": wallet,
                    "market": market_str,
                    "status": "opened_live",
                    "timestamp": trade.entry_timestamp,
                }
                _state["trade_log"].append(log_entry)
                await _broadcast({"type": "trade_opened", "trade": log_entry})
                
                msg = (
                    f"🚨 LIVE TRADE EXECUTED\n"
                    f"Wallet: {wallet[:6]}…{wallet[-4:]}\n"
                    f"Market: {market_str}\n"
                    f"Size: ${effective_size:.2f}  Price: {price:.4f}  Slippage: {slippage_pct*100:.2f}%\n"
                    f"TX Hash: {trade.evm_tx_hash}"
                )
                if confluence.multiplier > 1.0:
                    msg += f"\n🔥 Confluence Multiplier: {confluence.multiplier}x"
                
                await send_telegram_alert(msg)
        else:
            logger.warning("Unknown MODE: %s", MODE)



trade_detector = TradeDetectionService(market_cache, _on_trade_detected)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def _background_discovery_loop(interval_seconds: int = 300) -> None:
    """Refresh top traders and position sizes every *interval_seconds*."""
    while True:
        try:
            logger.info("Running wallet discovery …")
            # 1. Discover top traders
            traders = await discover_top_traders(market_cache=market_cache)
            
            # 2. Compute position sizes
            sizes = calculate_position_sizes(traders, PORTFOLIO_VALUE_USDC, KELLY_MODE)
            
            # 3. Update global state
            _state["top_traders"] = traders
            _state["position_sizes"] = sizes
            _state["sizing_map"] = {s["wallet"]: s["effective_size_usdc"] for s in sizes}
            _state["last_discovery"] = time.time()
            
            # 4. Update the detector with new wallets
            watched = [t["wallet"] for t in traders]
            trade_detector.set_watched_wallets(watched)
            
            await _broadcast({"type": "discovery_update", "count": len(traders)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Background discovery error: %s", exc)
        await asyncio.sleep(interval_seconds)


async def _background_market_resolution_loop(interval_seconds: int = 60) -> None:
    """Poll market cache to see if any open paper trades have resolved."""
    while True:
        try:
            async with async_session_maker() as session:
                repo = DBRepository(session)
                sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
                open_trades = await sim.open_trades()
                
                for trade in open_trades:
                    # Our market string format: "[cond_id] Title - Outcome"
                    if trade.market_title.startswith("[") and "]" in trade.market_title:
                        cond_id = trade.market_title[1:trade.market_title.find("]")]
                        resolved, winner = market_cache.get_resolution(cond_id)
                        
                        if resolved:
                            # Parse the outcome the user bet on from the " - Outcome" suffix
                            target_outcome = trade.market_title.split(" - ")[-1]
                            
                            # Compare user outcome with winner
                            user_won = (winner.lower() == target_outcome.lower()) if winner else False
                            
                            # In polymarket, winning shares pay $1. Losing ones pay $0.
                            exit_price = 1.0 if user_won else 0.0
                            
                            result = await sim.close_trade(trade.id, exit_price)
                            if result:
                                log_entry = {
                                    "trade_id": result.id,
                                    "outcome": result.closed_outcome,
                                    "realised_pnl_usdc": result.realised_pnl,
                                    "exit_price": result.exit_price,
                                }
                                await _broadcast({"type": "trade_closed", "trade": log_entry})
                                await send_telegram_alert(
                                    f"{'🟢' if result.closed_outcome == 'win' else '🔴'} Market Resolved\n"
                                    f"Outcome: {result.closed_outcome.upper()}  PnL: ${result.realised_pnl:.2f}\n"
                                    f"Market: {trade.market_title}"
                                )
                                
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Market resolution check error: %s", exc)
            
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
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Run background tasks while app is live."""
    await market_cache.start()
    await trade_detector.start()
    
    discovery_task = asyncio.create_task(_background_discovery_loop())
    resolution_task = asyncio.create_task(_background_market_resolution_loop())
    try:
        yield
    finally:
        discovery_task.cancel()
        resolution_task.cancel()
        await trade_detector.stop()
        await market_cache.stop()
        try:
            await discovery_task
            await resolution_task
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
    logger.info("Serving %d traders from state", len(_state["top_traders"]))
    return {
        "traders": _state["top_traders"],
        "last_updated": _state["last_discovery"],
        "count": len(_state["top_traders"]),
    }


@app.get("/api/positions")
async def get_positions() -> dict:
    """Return current active (paper) positions."""
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        open_pos = await sim.open_trades()
        
        return {
            "open_positions": [
                {
                    "trade_id": t.id,
                    "wallet": t.wallet,
                    "market": t.market_title,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "size_usdc": t.size_usdc,
                    "expected_ev_usdc": 0.0,
                    "timestamp": t.entry_timestamp,
                }
                for t in open_pos
            ],
            "count": len(open_pos),
        }


@app.get("/api/pnl")
async def get_pnl() -> dict:
    """Return realised and unrealised PnL summary."""
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        
        realised = await sim.total_realised_pnl()
        unrealised = await sim.unrealised_pnl()
        wr = await sim.win_rate()
        sharpe = await sim.sharpe_ratio()
        all_t = await repo.get_all_trades()
        
        return {
            "realised_pnl_usdc": realised,
            "unrealised_pnl_usdc": unrealised,
            "win_rate": wr,
            "max_drawdown_pct": round(sim.max_drawdown() * 100, 2),
            "sharpe_ratio": sharpe,
            "total_trades": len(all_t),
        }


@app.get("/api/portfolio")
async def get_portfolio() -> dict:
    """Return portfolio value and asset allocation."""
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        
        realised = await sim.total_realised_pnl()
        unrealised = await sim.unrealised_pnl()
        total_value = PORTFOLIO_VALUE_USDC + realised + unrealised
        
        return {
            "total_value_usdc": total_value,
            "cash_usdc": PORTFOLIO_VALUE_USDC + realised,
            "invested_usdc": sum(t.size_usdc for t in await sim.open_trades()),
            "currency": "USDC",
        }


@app.get("/api/trades")
async def get_trades(limit: int = 100) -> dict:
    """Return historical executed trades limit N."""
    async with async_session_maker() as session:
        repo = DBRepository(session)
        trades = await repo.get_all_trades(limit=limit)
        return {
            "trades": [
                {
                    "trade_id": t.id,
                    "wallet": t.wallet,
                    "market": t.market_title,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "size_usdc": t.size_usdc,
                    "realised_pnl": t.realised_pnl,
                    "status": t.status,
                    "category": t.category,
                    "timestamp": t.entry_timestamp,
                }
                for t in trades
            ]
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
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        return await sim.daily_summary()


@app.get("/api/performance-report")
async def get_performance_report() -> dict:
    """Alias for /api/report for backward compatibility/standardization."""
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        return await sim.daily_summary()


@app.post("/api/trades/open")
async def open_paper_trade(trade: dict) -> dict:
    """
    Open a new paper trade manually (mostly for testing).
    """
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        
        result = await sim.record_trade(
            trade_id=trade.get("trade_id", str(time.time())),
            wallet=trade.get("wallet", ""),
            market=trade.get("market", ""),
            side=trade.get("side", "BUY"),
            nominal_price=float(trade.get("entry_price", 0)),
            size_usdc=float(trade.get("size_usdc", 0)),
            expected_ev_usdc=float(trade.get("expected_ev_usdc", 0)),
            category=_classify_market(trade.get("market", "")),
        )
        if result is None:
            raise HTTPException(status_code=400, detail="Insufficient balance or invalid trade")

        log_entry = {
            "trade_id": result.id,
            "wallet": result.wallet,
            "market": result.market_title,
            "status": "opened",
            "timestamp": result.entry_timestamp,
        }
        _state["trade_log"].append(log_entry)
        await _broadcast({"type": "trade_opened", "trade": log_entry})
        await send_telegram_alert(
            f"✅ Manual paper trade opened\n"
            f"Market: {result.market_title}\n"
            f"Side: {result.side}  Size: ${result.size_usdc:.2f}  Price: {result.entry_price:.4f}"
        )
        return log_entry


@app.post("/api/trades/close")
async def close_paper_trade(payload: dict) -> dict:
    """
    Close an existing paper trade manually.
    """
    async with async_session_maker() as session:
        repo = DBRepository(session)
        sim = PaperTradingSimulator(repo, starting_balance=PORTFOLIO_VALUE_USDC)
        
        trade_id = payload.get("trade_id", "")
        exit_price = float(payload.get("exit_price", 0))
        result = await sim.close_trade(trade_id, exit_price)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

        log_entry = {
            "trade_id": result.id,
            "outcome": result.closed_outcome,
            "realised_pnl_usdc": result.realised_pnl,
            "exit_price": result.exit_price,
        }
        await _broadcast({"type": "trade_closed", "trade": log_entry})
        await send_telegram_alert(
            f"{'🟢' if result.closed_outcome == 'win' else '🔴'} Manual trade closed\n"
            f"Outcome: {result.closed_outcome.upper()}  PnL: ${result.realised_pnl:.2f}"
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
    """
    await ws.accept()
    _ws_clients.add(ws)
    logger.info("WebSocket client connected (%d total)", len(_ws_clients))
    try:
        # Send current state on connect
        async with async_session_maker() as session:
            repo = DBRepository(session)
            open_pos = await repo.get_open_positions()
                            
        await ws.send_text(json.dumps({
            "type": "init",
            "traders_count": len(_state["top_traders"]),
            "open_positions": len(open_pos),
        }))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(_ws_clients))
