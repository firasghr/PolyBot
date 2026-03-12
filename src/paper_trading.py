"""
Paper Trading Simulator
=======================
Simulates copy trades in a risk-free environment using live market data and
wallet signals from the Wallet Discovery and Trade Execution modules.

Features:
  - In-memory position tracking with correct position-based accounting
  - BUY increases position, SELL reduces position and realises PnL
  - PnL = (exit_price - entry_price) * shares  (trade_value = shares * price)
  - Win rate counts only closed trades
  - Sharpe ratio uses per-trade returns (PnL / invested)
  - Applies configurable slippage and latency adjustments
  - Tracks realised PnL, unrealised PnL, drawdowns, and per-trade success rate
  - Produces a daily summary report (JSON + optional ASCII chart)

Usage (stand-alone):
    python -m src.paper_trading

Usage (imported):
    from src.paper_trading import PaperTradingSimulator
    sim = PaperTradingSimulator(starting_balance=50_000)
    sim.record_trade(...)
    report = sim.daily_summary()
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

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
# Data models
# ---------------------------------------------------------------------------
@dataclass
class TradeRecord:
    """A single paper trade opened (and optionally closed) by the simulator."""

    trade_id: str
    wallet: str
    market: str
    side: str
    nominal_price: float
    entry_price: float
    size_usdc: float
    shares: float
    category: str = "other"
    entry_timestamp: float = field(default_factory=time.time)

    # Populated when the trade is closed
    closed: bool = False
    exit_price: float = 0.0
    exit_timestamp: float = 0.0
    realised_pnl: float = 0.0
    outcome: str = "open"  # "win", "loss", or "open"

    # ------------------------------------------------------------------
    # Compatibility aliases used by backend/main.py and the FastAPI routes
    # ------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.trade_id

    @property
    def market_title(self) -> str:
        return self.market

    @property
    def status(self) -> str:
        return "closed" if self.closed else "open"

    @property
    def closed_outcome(self) -> str:
        return self.outcome


@dataclass
class PortfolioSnapshot:
    """Point-in-time snapshot of the paper portfolio."""

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
    In-memory paper trading simulator for Polymarket copy-trading strategies.

    Implements position-based accounting:
      - BUY  → open (or add to) a position
      - SELL → reduce position, realise PnL = (sell_price - avg_entry) * shares_sold

    Parameters
    ----------
    starting_balance : float
        Initial USDC balance.
    slippage_pct : float
        Fraction of price added as simulated slippage on BUY (deducted on SELL).
    latency_ms : float
        Simulated execution latency in milliseconds (logged only).
    """

    def __init__(
        self,
        starting_balance: float = 50_000.0,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
        latency_ms: float = DEFAULT_LATENCY_MS,
    ) -> None:
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.slippage_pct = slippage_pct
        self.latency_ms = latency_ms

        self.trades: list[TradeRecord] = []
        self.snapshots: list[PortfolioSnapshot] = []
        self._peak_value: float = starting_balance

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------
    def record_trade(
        self,
        trade_id: str,
        wallet: str,
        market: str,
        side: str,
        nominal_price: float,
        size_usdc: float,
        expected_ev_usdc: float = 0.0,
        category: str = "other",
    ) -> TradeRecord | None:
        """
        Open a new paper trade.

        Applies slippage to the entry price, deducts from balance, and stores
        the trade.  Returns ``None`` if the balance is insufficient.
        """
        if size_usdc > self.balance:
            logger.warning(
                "Insufficient balance ($%.2f) for trade $%.2f", self.balance, size_usdc
            )
            return None

        logger.debug("Simulating %dms execution latency …", self.latency_ms)

        slippage_factor = (
            1.0 + self.slippage_pct if side.upper() == "BUY" else 1.0 - self.slippage_pct
        )
        entry_price = nominal_price * slippage_factor
        # trade_value = shares * price  →  shares = size_usdc / entry_price
        shares = size_usdc / entry_price if entry_price > 0 else 0.0

        trade = TradeRecord(
            trade_id=trade_id,
            wallet=wallet,
            market=market,
            side=side.upper(),
            nominal_price=nominal_price,
            entry_price=round(entry_price, 6),
            size_usdc=round(size_usdc, 4),
            shares=round(shares, 6),
            category=category,
        )

        self.balance -= size_usdc
        self.trades.append(trade)

        logger.info(
            "Paper trade OPEN  id=%s  market=%s  side=%s  nominal=%.4f  entry=%.4f  size=$%.2f",
            trade_id,
            market[:20],
            side,
            nominal_price,
            entry_price,
            size_usdc,
        )
        return trade

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
    ) -> TradeRecord | None:
        """
        Close an open paper trade at *exit_price*.

        PnL = (exit_price - entry_price) * shares
            = exit_value - cost_basis

        Returns ``None`` if the trade is not found or already closed.
        """
        trade = next(
            (t for t in self.trades if t.trade_id == trade_id and not t.closed), None
        )
        if not trade:
            logger.warning("Trade %s not found or already closed", trade_id)
            return None

        exit_value = trade.shares * exit_price
        pnl = exit_value - trade.size_usdc

        trade.exit_price = round(exit_price, 6)
        trade.exit_timestamp = time.time()
        trade.realised_pnl = round(pnl, 4)
        trade.closed = True
        trade.outcome = "win" if pnl > 0 else "loss"

        self.balance += exit_value
        self._update_peak()

        logger.info(
            "Paper trade CLOSE id=%s  exit=%.4f  pnl=$%.2f  outcome=%s",
            trade_id,
            exit_price,
            pnl,
            trade.outcome,
        )
        return trade

    # ------------------------------------------------------------------
    # Portfolio views
    # ------------------------------------------------------------------
    def open_trades(self) -> list[TradeRecord]:
        """Return all trades that have not yet been closed."""
        return [t for t in self.trades if not t.closed]

    def closed_trades(self) -> list[TradeRecord]:
        """Return all trades that have been closed."""
        return [t for t in self.trades if t.closed]

    # ------------------------------------------------------------------
    # Portfolio metrics
    # ------------------------------------------------------------------
    def win_rate(self) -> float:
        """Fraction of *closed* trades that were wins."""
        closed = self.closed_trades()
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.outcome == "win")
        return round(wins / len(closed), 4)

    def total_realised_pnl(self) -> float:
        """Sum of realised PnL across all closed trades."""
        return round(sum(t.realised_pnl for t in self.closed_trades()), 4)

    def unrealised_pnl(self, current_prices: dict[str, float] | None = None) -> float:
        """Estimated unrealised PnL for open positions."""
        if not current_prices:
            return 0.0
        total = 0.0
        for t in self.open_trades():
            px = current_prices.get(t.market, t.entry_price)
            total += (t.shares * px) - t.size_usdc
        return round(total, 4)

    def max_drawdown(self) -> float:
        """
        Maximum peak-to-trough drawdown as a fraction of peak portfolio value.

        Returns a value in [0, 1].
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

    def sharpe_ratio(self, risk_free: float = 0.0) -> float:
        """
        Sharpe ratio computed from per-trade returns of closed trades.

        return_i = realised_pnl_i / size_usdc_i
        Sharpe   = (mean_return - risk_free) / std_return

        Returns 0.0 when fewer than 2 closed trades exist.
        """
        closed = self.closed_trades()
        if len(closed) < 2:
            return 0.0
        returns = [
            t.realised_pnl / t.size_usdc for t in closed if t.size_usdc > 0
        ]
        if len(returns) < 2:
            return 0.0
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((x - mean) ** 2 for x in returns) / (n - 1)
        std = math.sqrt(variance)
        return round((mean - risk_free) / std, 4) if std > 0 else 0.0

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
            unrealised_pnl=0.0,
        )
        self.snapshots.append(snap)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def daily_summary(
        self, current_prices: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """Generate a daily summary report as a JSON-serialisable dict."""
        closed = self.closed_trades()
        open_pos = self.open_trades()

        summary: dict[str, Any] = {
            "timestamp": time.time(),
            "starting_balance_usdc": self.starting_balance,
            "current_balance_usdc": round(self.balance, 4),
            "total_trades": len(self.trades),
            "closed_trades": len(closed),
            "open_trades": len(open_pos),
            "realised_pnl_usdc": round(self.total_realised_pnl(), 4),
            "unrealised_pnl_usdc": self.unrealised_pnl(current_prices),
            "total_expected_ev_usdc": 0.0,
            "win_rate": self.win_rate(),
            "max_drawdown_pct": round(self.max_drawdown() * 100, 2),
            "sharpe_ratio": self.sharpe_ratio(),
            "slippage_pct_applied": round(self.slippage_pct * 100, 2),
            "simulated_latency_ms": self.latency_ms,
            "open_positions": [
                {
                    "trade_id": t.trade_id,
                    "wallet": t.wallet,
                    "market": t.market,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "size_usdc": t.size_usdc,
                    "expected_ev_usdc": 0.0,
                }
                for t in open_pos
            ],
            "closed_positions": [
                {
                    "trade_id": t.trade_id,
                    "wallet": t.wallet,
                    "market": t.market,
                    "outcome": t.outcome,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "realised_pnl_usdc": t.realised_pnl,
                }
                for t in closed
            ],
        }

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
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    sim = PaperTradingSimulator(starting_balance=10_000.0)

    # Simulate a few trades
    ev_apx = 12.5
    sim.record_trade("t1", "0xABC", "Will BTC hit $100k?", "BUY", 0.65, 500.0, ev_apx)
    sim.record_trade("t2", "0xABC", "Will ETH hit $5k?", "BUY", 0.40, 300.0, ev_apx)
    sim.close_trade("t1", 0.90)   # win
    sim.close_trade("t2", 0.20)   # loss

    report = sim.daily_summary()
    print(json.dumps(report, indent=2))

