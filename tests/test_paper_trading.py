"""Tests for the Paper Trading Simulator module."""

from __future__ import annotations

import pytest

from src.paper_trading import PaperTradingSimulator, _sparkline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sim():
    """Fresh simulator with $10k starting balance."""
    return PaperTradingSimulator(starting_balance=10_000.0, slippage_pct=0.02)


@pytest.fixture
def sim_with_trades(sim):
    """Simulator with two trades: one win and one loss."""
    sim.record_trade("t1", "0xABC", "Will BTC hit $100k?", "BUY", 0.65, 500.0, 20.0)
    sim.record_trade("t2", "0xABC", "Will ETH hit $5k?", "BUY", 0.40, 300.0, 5.0)
    sim.close_trade("t1", 0.90)   # win
    sim.close_trade("t2", 0.20)   # loss
    return sim


# ---------------------------------------------------------------------------
# record_trade tests
# ---------------------------------------------------------------------------
class TestRecordTrade:
    def test_opens_trade_successfully(self, sim):
        trade = sim.record_trade("t1", "0xABC", "Some market", "BUY", 0.5, 100.0)
        assert trade is not None
        assert trade.trade_id == "t1"
        assert trade.closed is False

    def test_deducts_from_balance(self, sim):
        initial_balance = sim.balance
        sim.record_trade("t1", "0xABC", "Some market", "BUY", 0.5, 1000.0)
        assert sim.balance == initial_balance - 1000.0

    def test_applies_slippage_on_buy(self, sim):
        trade = sim.record_trade("t1", "0xABC", "Some market", "BUY", 0.5, 100.0)
        # Entry price should be higher than nominal due to BUY slippage
        assert trade.entry_price > trade.nominal_price

    def test_applies_slippage_on_sell(self, sim):
        trade = sim.record_trade("t1", "0xABC", "Some market", "SELL", 0.8, 100.0)
        # Entry price should be lower than nominal due to SELL slippage
        assert trade.entry_price < trade.nominal_price

    def test_rejects_trade_exceeding_balance(self, sim):
        result = sim.record_trade("t1", "0xABC", "Some market", "BUY", 0.5, 999_999.0)
        assert result is None
        assert len(sim.trades) == 0


# ---------------------------------------------------------------------------
# close_trade tests
# ---------------------------------------------------------------------------
class TestCloseTrade:
    def test_close_win(self, sim):
        sim.record_trade("t1", "0xABC", "Market", "BUY", 0.5, 100.0)
        # Exit at higher price → win
        result = sim.close_trade("t1", 0.9)
        assert result is not None
        assert result.outcome == "win"
        assert result.realised_pnl > 0

    def test_close_loss(self, sim):
        sim.record_trade("t1", "0xABC", "Market", "BUY", 0.8, 100.0)
        # Exit at lower price → loss
        result = sim.close_trade("t1", 0.1)
        assert result is not None
        assert result.outcome == "loss"
        assert result.realised_pnl < 0

    def test_close_nonexistent_returns_none(self, sim):
        result = sim.close_trade("nonexistent", 0.5)
        assert result is None

    def test_close_already_closed_returns_none(self, sim):
        sim.record_trade("t1", "0xABC", "Market", "BUY", 0.5, 100.0)
        sim.close_trade("t1", 0.9)
        result = sim.close_trade("t1", 0.9)  # second close attempt
        assert result is None


# ---------------------------------------------------------------------------
# Portfolio metrics
# ---------------------------------------------------------------------------
class TestPortfolioMetrics:
    def test_win_rate(self, sim_with_trades):
        wr = sim_with_trades.win_rate()
        # one win, one loss → 50%
        assert wr == pytest.approx(0.5)

    def test_total_realised_pnl(self, sim_with_trades):
        """Realised PnL is the sum of all closed trades' PnL."""
        pnl = sim_with_trades.total_realised_pnl()
        expected = sum(t.realised_pnl for t in sim_with_trades.closed_trades())
        assert pnl == pytest.approx(expected)

    def test_max_drawdown_non_negative(self, sim_with_trades):
        dd = sim_with_trades.max_drawdown()
        assert 0.0 <= dd <= 1.0

    def test_sharpe_needs_two_trades(self, sim):
        # With no closed trades, Sharpe is 0
        assert sim.sharpe_ratio() == 0.0
        sim.record_trade("t1", "0xABC", "M1", "BUY", 0.5, 100.0)
        sim.close_trade("t1", 0.9)
        # Still only one closed trade
        assert sim.sharpe_ratio() == 0.0

    def test_sharpe_computed_with_multiple_trades(self, sim_with_trades):
        # With mixed trades, Sharpe should be a float (could be positive, zero, or negative)
        sharpe = sim_with_trades.sharpe_ratio()
        assert isinstance(sharpe, float)

    def test_open_trades_count(self, sim):
        sim.record_trade("t1", "0xABC", "M1", "BUY", 0.5, 100.0)
        sim.record_trade("t2", "0xABC", "M2", "BUY", 0.6, 200.0)
        assert len(sim.open_trades()) == 2

    def test_open_reduces_after_close(self, sim):
        sim.record_trade("t1", "0xABC", "M1", "BUY", 0.5, 100.0)
        sim.record_trade("t2", "0xABC", "M2", "BUY", 0.6, 200.0)
        sim.close_trade("t1", 0.8)
        assert len(sim.open_trades()) == 1
        assert len(sim.closed_trades()) == 1


# ---------------------------------------------------------------------------
# Daily summary report
# ---------------------------------------------------------------------------
class TestDailySummary:
    def test_summary_contains_required_keys(self, sim_with_trades):
        report = sim_with_trades.daily_summary()
        required_keys = [
            "timestamp", "starting_balance_usdc", "current_balance_usdc",
            "total_trades", "closed_trades", "open_trades",
            "realised_pnl_usdc", "unrealised_pnl_usdc",
            "win_rate", "max_drawdown_pct", "sharpe_ratio",
        ]
        for key in required_keys:
            assert key in report, f"Missing key: {key}"

    def test_summary_trade_counts_consistent(self, sim_with_trades):
        report = sim_with_trades.daily_summary()
        assert report["closed_trades"] == 2
        assert report["open_trades"] == 0
        assert report["total_trades"] == 2


# ---------------------------------------------------------------------------
# Sparkline helper
# ---------------------------------------------------------------------------
class TestSparkline:
    def test_empty_returns_empty_string(self):
        assert _sparkline([]) == ""

    def test_returns_string(self):
        result = _sparkline([1.0, 2.0, 3.0, 4.0])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_constant_series(self):
        """All identical values → single bar type (no crash)."""
        result = _sparkline([5.0, 5.0, 5.0])
        assert isinstance(result, str)
        assert len(result) == 3
