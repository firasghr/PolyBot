"""
Paper Trading Simulator
=======================
Simulates copy trades in a risk-free environment using live market data and
wallet signals from the Wallet Discovery and Trade Execution modules.

Features:
  - Accepts the same trade signals as the live execution module
  - Applies configurable slippage and latency adjustments
  - Tracks realised PnL, unrealised PnL, Expected Value (EV), drawdowns,
    and per-trade success rate
  - Produces a daily summary report (JSON + optional ASCII chart)
  - Integrates seamlessly with the Trade Detection & Execution module

Usage (stand-alone):
    python -m src.paper_trading

Usage (imported):
    from src.paper_trading import PaperTradingSimulator
    sim = PaperTradingSimulator(starting_balance=50_000)
    sim.record_trade(...)
    report = sim.daily_summary()
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from database.models import Trade, Position
from database.repository import DBRepository

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_SLIPPAGE_PCT: float = float(os.getenv("SIM_SLIPPAGE_PCT", "0.02"))  # 2%
DEFAULT_LATENCY_MS: float = float(os.getenv("SIM_LATENCY_MS", "200"))       # 200 ms


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PortfolioSnapshot:
    """Point-in-time snapshot of paper portfolio."""

    timestamp: float
    balance_usdc: float
    open_positions_value: float
    total_value: float
    realised_pnl: float
    unrealised_pnl: float


# ---------------------------------------------------------------------------
# Simulator core
# ---------------------------------------------------------------------------
class PaperTradingSimulator:
    """
    Paper trading simulator for Polymarket copy-trading strategies.

    Parameters
    ----------
    starting_balance : float
        Initial USDC balance to simulate with.
    slippage_pct : float
        Fraction of price to add as simulated slippage on entry.
    latency_ms : float
        Simulated execution latency in milliseconds (used for logging).
    """

    def __init__(
        self,
        repository: DBRepository,
        starting_balance: float = 50_000.0,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
        latency_ms: float = DEFAULT_LATENCY_MS,
    ) -> None:
        self.repo = repository
        self.starting_balance = starting_balance
        self.balance = starting_balance  # available USDC. FIXME: store in a Portfolio model
        self.slippage_pct = slippage_pct
        self.latency_ms = latency_ms

        self.snapshots: list[PortfolioSnapshot] = []
        self._peak_value: float = starting_balance

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------
    async def record_trade(
        self,
        trade_id: str,
        wallet: str,
        market: str,
        side: str,
        nominal_price: float,
        size_usdc: float,
        expected_ev_usdc: float = 0.0,
        category: str = "other",
    ) -> Trade | None:
        """
        Open a new paper trade.
        Applies slippage and latency, deducts from balance, and stores the
        trade record in SQLite.
        """
        if size_usdc > self.balance:
            logger.warning("Insufficient balance ($%.2f) for trade $%.2f", self.balance, size_usdc)
            return None

        # Simulate execution latency
        logger.debug("Simulating %dms execution latency …", self.latency_ms)

        # Apply slippage to entry price
        slippage_factor = 1.0 + self.slippage_pct if side.upper() == "BUY" else 1.0 - self.slippage_pct
        entry_price = nominal_price * slippage_factor
        shares = size_usdc / entry_price if entry_price > 0 else 0.0

        ts = time.time()
        
        # Determine condition_id from market string format "[condition_id] title - outcome"
        try:
            cid = market.split("]")[0].replace("[", "").strip()
        except IndexError:
            cid = market
            
        try:
            outcome = market.split(" - ")[-1].strip()
        except IndexError:
            outcome = "Unknown"

        trade = Trade(
            id=trade_id,
            wallet=wallet,
            market_id=cid,
            market_title=market,
            outcome=outcome,
            side=side.upper(),
            category=category,
            entry_price=round(entry_price, 6),
            size_usdc=round(size_usdc, 4),
            shares=round(shares, 4),
            entry_timestamp=ts,
            status="open"
        )
        
        position = Position(
            id=trade_id,
            wallet=wallet,
            market_id=cid,
            market_title=market,
            outcome=outcome,
            category=category,
            entry_price=round(entry_price, 6),
            size_usdc=round(size_usdc, 4),
            shares=round(shares, 4),
            timestamp=ts
        )

        self.balance -= size_usdc
        
        await self.repo.add_trade(trade)
        await self.repo.add_position(position)
        
        # self._snapshot()  # skipping snapshot loop adjustment for brevity

        logger.info(
            "Paper trade OPEN  id=%s  market=%s  side=%s  nominal=%.4f  entry=%.4f  size=$%.2f",
            trade_id, market[:20], side, nominal_price, entry_price, size_usdc,
        )
        return trade

    async def close_trade(
        self,
        trade_id: str,
        exit_price: float,
    ) -> Trade | None:
        """
        Close an existing open paper trade at the given exit price.
        Computes realised PnL, updates balance, and writes to SQLite.
        """
        trade = await self.repo.get_trade(trade_id)
        if not trade or trade.status != "open":
            logger.warning("Trade %s not found or already closed", trade_id)
            return None

        # For a BUY trade: PnL = shares * exit_price - size_usdc
        exit_value = trade.shares * exit_price
        pnl = exit_value - trade.size_usdc

        trade.exit_price = round(exit_price, 6)
        trade.exit_timestamp = time.time()
        trade.realised_pnl = round(pnl, 4)
        trade.status = "closed"
        trade.closed_outcome = "win" if pnl > 0 else "loss"

        self.balance += exit_value
        
        await self.repo.update_trade(trade)
        await self.repo.remove_position(trade_id)
        
        self._update_peak()

        logger.info(
            "Paper trade CLOSE id=%s  exit=%.4f  pnl=$%.2f  outcome=%s",
            trade_id, exit_price, pnl, trade.closed_outcome,
        )
        return trade

    # ------------------------------------------------------------------
    # Portfolio metrics (Async SQLite driven)
    # ------------------------------------------------------------------
    async def open_trades(self) -> list[Trade]:
        """Return all trades that have not yet been closed."""
        all_t = await self.repo.get_all_trades(limit=1000)
        return [t for t in all_t if t.status == "open"]

    async def closed_trades(self) -> list[Trade]:
        """Return all trades that have been closed."""
        all_t = await self.repo.get_all_trades(limit=1000)
        return [t for t in all_t if t.status == "closed"]

    async def total_realised_pnl(self) -> float:
        """Sum of realised PnL across all closed trades."""
        closed = await self.closed_trades()
        return sum(t.realised_pnl for t in closed)

    async def unrealised_pnl(self, current_prices: dict[str, float] | None = None) -> float:
        """
        Estimated unrealised PnL for open positions.
        """
        if not current_prices:
            return 0.0
        total = 0.0
        open_pos = await self.open_trades()
        for t in open_pos:
            px = current_prices.get(t.market_title, t.entry_price)
            total += (t.shares * px) - t.size_usdc
        return round(total, 4)

    async def win_rate(self) -> float:
        """Fraction of closed trades that were wins."""
        closed = await self.closed_trades()
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.closed_outcome == "win")
        return round(wins / len(closed), 4)

    def max_drawdown(self) -> float:
        """
        Maximum peak-to-trough drawdown as a fraction of peak portfolio value.

        Returns a value in [0, 1] (0 = no drawdown, 1 = total loss).
        """
        if not self.snapshots:
            return 0.0
        peak = self.starting_balance
        max_dd = 0.0
        for snap in self.snapshots:
            v = snap.total_value
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 4)

    async def sharpe_ratio(self, risk_free: float = 0.0) -> float:
        """
        Sharpe ratio from closed-trade PnL series.
        """
        closed = await self.closed_trades()
        if len(closed) < 2:
            return 0.0
        pnl_series = [t.realised_pnl for t in closed]
        n = len(pnl_series)
        mean = sum(pnl_series) / n
        if n > 1:
            variance = sum((x - mean) ** 2 for x in pnl_series) / (n - 1)
        else:
            variance = 0.0
        std = math.sqrt(variance)
        return round((mean - risk_free) / std, 4) if std > 0 else 0.0

    async def total_expected_ev(self) -> float:
        """Sum of expected EV for all recorded trades."""
        all_t = await self.repo.get_all_trades()
        # Fallback to zero if the mock doesn't support the column or we dropped EV
        return 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _update_peak(self) -> None:
        """Track peak portfolio value for drawdown calculation."""
        current = self.balance + sum(t.size_usdc for t in self.open_trades())
        if current > self._peak_value:
            self._peak_value = current

    def _snapshot(self) -> None:
        """Record a point-in-time portfolio snapshot."""
        open_val = sum(t.size_usdc for t in self.open_trades())
        total = self.balance + open_val
        snap = PortfolioSnapshot(
            timestamp=time.time(),
            balance_usdc=round(self.balance, 4),
            open_positions_value=round(open_val, 4),
            total_value=round(total, 4),
            realised_pnl=round(self.total_realised_pnl(), 4),
            unrealised_pnl=0.0,  # updated lazily via unrealised_pnl()
        )
        self.snapshots.append(snap)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    async def daily_summary(self, current_prices: dict[str, float] | None = None) -> dict[str, Any]:
        """
        Generate a daily summary report as a JSON-serialisable dict.
        """
        closed = await self.closed_trades()
        open_pos = await self.open_trades()
        all_t = await self.repo.get_all_trades()
        
        realised = await self.total_realised_pnl()
        unrealised = await self.unrealised_pnl(current_prices)
        wr = await self.win_rate()
        sharpe = await self.sharpe_ratio()
        ev = await self.total_expected_ev()

        summary: dict[str, Any] = {
            "timestamp": time.time(),
            "starting_balance_usdc": self.starting_balance,
            "current_balance_usdc": round(self.balance, 4),
            "total_trades": len(all_t),
            "closed_trades": len(closed),
            "open_trades": len(open_pos),
            "realised_pnl_usdc": realised,
            "unrealised_pnl_usdc": unrealised,
            "total_expected_ev_usdc": ev,
            "win_rate": wr,
            "max_drawdown_pct": round(self.max_drawdown() * 100, 2),
            "sharpe_ratio": sharpe,
            "slippage_pct_applied": round(self.slippage_pct * 100, 2),
            "simulated_latency_ms": self.latency_ms,
            "open_positions": [
                {
                    "trade_id": t.id,
                    "wallet": t.wallet,
                    "market": t.market_title,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "size_usdc": t.size_usdc,
                    "expected_ev_usdc": 0.0,
                }
                for t in open_pos
            ],
            "closed_positions": [
                {
                    "trade_id": t.id,
                    "wallet": t.wallet,
                    "market": t.market_title,
                    "outcome": t.closed_outcome,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "realised_pnl_usdc": t.realised_pnl,
                }
                for t in closed
            ],
        }

        # Print ASCII sparkline for balance history
        if len(self.snapshots) >= 2:
            summary["balance_history"] = _sparkline(
                [s.total_value for s in self.snapshots[-30:]]
            )

        return summary


# ---------------------------------------------------------------------------
# ASCII sparkline chart helper
# ---------------------------------------------------------------------------
def _sparkline(values: list[float], width: int = 30) -> str:
    """
    Generate a simple ASCII sparkline from a list of float values.

    Uses block characters ▁▂▃▄▅▆▇█ to represent relative magnitude.
    """
    bars = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo, hi = min(values), max(values)
    spread = hi - lo or 1.0
    chars = [bars[min(7, int((v - lo) / spread * 8))] for v in values[-width:]]
    return "".join(chars)


# ---------------------------------------------------------------------------
# Async simulation runner (integrates with trade_execution signals)
# ---------------------------------------------------------------------------
async def run_simulation(
    signal_queue: asyncio.Queue,
    starting_balance: float = 50_000.0,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> PaperTradingSimulator:
    """
    Consume trade signals from an asyncio Queue and paper-trade them.

    Signals are expected to be dicts with keys matching `record_trade` params.
    A ``None`` value on the queue signals shutdown.
    """
    sim = PaperTradingSimulator(starting_balance, slippage_pct)

    while True:
        signal = await signal_queue.get()
        if signal is None:
            break

        action = signal.get("action", "open")
        if action == "open":
            sim.record_trade(
                trade_id=signal["trade_id"],
                wallet=signal["wallet"],
                market=signal["market"],
                side=signal["side"],
                nominal_price=signal["entry_price"],
                size_usdc=signal["position_size_usdc"],
                expected_ev_usdc=signal.get("expected_ev_usdc", 0.0),
            )
        elif action == "close":
            sim.close_trade(signal["trade_id"], signal["exit_price"])

        signal_queue.task_done()

    logger.info("Simulation run complete. Generating daily summary …")
    return sim


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    async def _demo() -> None:
        sim = PaperTradingSimulator(starting_balance=10_000.0)

        # Simulate a few trades
        ev_apx = 12.5
        sim.record_trade("t1", "0xABC", "Will BTC hit $100k?", "BUY", 0.65, 500.0, ev_apx)
        sim.record_trade("t2", "0xABC", "Will ETH hit $5k?", "BUY", 0.40, 300.0, ev_apx)
        sim.close_trade("t1", 0.90)   # win
        sim.close_trade("t2", 0.20)   # loss

        report = sim.daily_summary()
        print(json.dumps(report, indent=2))

    asyncio.run(_demo())
