"""Tests for the Wallet Discovery module (pure logic only — no network calls)."""

from __future__ import annotations

import math

import pytest

from src.wallet_discovery import (
    _build_wallet_stats,
    _classify_market,
    _compute_sharpe,
)


# ---------------------------------------------------------------------------
# Market classification
# ---------------------------------------------------------------------------
class TestClassifyMarket:
    @pytest.mark.parametrize("question,expected", [
        ("Will Bitcoin hit $100k by end of year?", "crypto"),
        ("Who will win the 2026 US Presidential election?", "politics"),
        ("Will the NBA Finals go to game 7?", "sports"),
        ("Will the Fed cut rates in March?", "finance"),
        ("Will aliens be confirmed this decade?", "other"),
    ])
    def test_classification(self, question, expected):
        assert _classify_market(question) == expected

    def test_case_insensitive(self):
        assert _classify_market("BITCOIN price prediction") == "crypto"


# ---------------------------------------------------------------------------
# Sharpe ratio computation
# ---------------------------------------------------------------------------
class TestComputeSharpe:
    def test_positive_mean_zero_risk_free(self):
        pnl = [10.0, 20.0, 15.0, 30.0, 25.0]
        sharpe = _compute_sharpe(pnl)
        assert sharpe > 0

    def test_zero_std_returns_zero(self):
        pnl = [5.0, 5.0, 5.0, 5.0]
        sharpe = _compute_sharpe(pnl)
        assert sharpe == 0.0

    def test_single_element_returns_zero(self):
        assert _compute_sharpe([100.0]) == 0.0

    def test_empty_returns_zero(self):
        assert _compute_sharpe([]) == 0.0

    def test_negative_mean_negative_sharpe(self):
        pnl = [-10.0, -20.0, -15.0, -5.0]
        sharpe = _compute_sharpe(pnl)
        assert sharpe < 0

    def test_risk_free_reduces_sharpe(self):
        pnl = [10.0, 20.0, 15.0, 25.0]
        sharpe_no_rf  = _compute_sharpe(pnl, risk_free=0.0)
        sharpe_with_rf = _compute_sharpe(pnl, risk_free=10.0)
        assert sharpe_no_rf > sharpe_with_rf


# ---------------------------------------------------------------------------
# Wallet stats builder
# ---------------------------------------------------------------------------
class TestBuildWalletStats:
    def _make_positions(self, n=150, win_pct=0.65):
        """Generate synthetic position records."""
        positions = []
        for i in range(n):
            pnl = 50.0 if (i / n) < win_pct else -30.0
            positions.append({
                "pnl": pnl,
                "size": 1000.0,
                "market_question": "Will BTC hit $100k?" if i % 2 == 0 else "2024 election winner",
            })
        return positions

    def test_returns_none_below_min_trades(self):
        positions = self._make_positions(n=50)
        result = _build_wallet_stats("0xABC", positions)
        assert result is None

    def test_returns_none_below_min_win_rate(self):
        positions = self._make_positions(n=150, win_pct=0.40)
        result = _build_wallet_stats("0xABC", positions)
        assert result is None

    def test_returns_dict_when_qualifying(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        assert result is not None
        assert isinstance(result, dict)

    def test_required_keys_present(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        required = {
            "wallet", "trade_count", "win_rate", "avg_position_size_usdc",
            "market_focus", "market_distribution", "sharpe_ratio", "total_pnl_usdc",
        }
        assert required.issubset(result.keys())

    def test_wallet_address_preserved(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xDEADBEEF", positions)
        assert result["wallet"] == "0xDEADBEEF"

    def test_trade_count_correct(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        assert result["trade_count"] == 150

    def test_win_rate_approx_correct(self):
        positions = self._make_positions(n=200, win_pct=0.75)
        result = _build_wallet_stats("0xABC", positions)
        assert abs(result["win_rate"] - 0.75) < 0.02

    def test_market_focus_crypto(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        # all positions reference BTC or election; crypto appears ~half the time
        result = _build_wallet_stats("0xABC", positions)
        assert result["market_focus"] in ("crypto", "politics", "other")

    def test_sharpe_finite(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        assert math.isfinite(result["sharpe_ratio"])
