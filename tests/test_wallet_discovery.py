"""Tests for the Wallet Discovery module (pure logic only — no network calls)."""

from __future__ import annotations

import math

import pytest

from src.wallet_discovery import (
    _build_wallet_stats,
    _classify_market,
    _compute_sharpe,
    _compute_wallet_score,
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
# Composite scoring
# ---------------------------------------------------------------------------
class TestComputeWalletScore:
    def test_perfect_score(self):
        stats = {"win_rate": 1.0, "sharpe_ratio": 3.0, "total_pnl_usdc": 10000, "trade_count": 500}
        score = _compute_wallet_score(stats)
        assert score == pytest.approx(1.0, abs=0.01)

    def test_zero_score(self):
        stats = {"win_rate": 0.0, "sharpe_ratio": 0.0, "total_pnl_usdc": 0, "trade_count": 0}
        score = _compute_wallet_score(stats)
        assert score == 0.0

    def test_mid_range(self):
        stats = {"win_rate": 0.65, "sharpe_ratio": 1.5, "total_pnl_usdc": 5000, "trade_count": 250}
        score = _compute_wallet_score(stats)
        assert 0.3 < score < 0.7


# ---------------------------------------------------------------------------
# Wallet stats builder
# ---------------------------------------------------------------------------
class TestBuildWalletStats:
    def _make_positions(self, n=150, win_pct=0.65):
        """Generate synthetic position records with real win/loss PnL."""
        positions = []
        for i in range(n):
            if (i / n) < win_pct:
                pnl = 50.0   # winning trade
            else:
                pnl = -30.0  # losing trade
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
            "wallet", "name", "pseudonym", "profile_image", "bio",
            "trade_count", "decided_trades", "win_rate",
            "avg_position_size_usdc", "avg_win_usdc", "avg_loss_usdc",
            "market_focus", "market_distribution", "sharpe_ratio",
            "total_pnl_usdc", "composite_score",
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

    def test_avg_win_loss_computed(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        assert result["avg_win_usdc"] > 0
        assert result["avg_loss_usdc"] > 0

    def test_composite_score_present(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        assert 0.0 <= result["composite_score"] <= 1.0

    def test_sharpe_finite(self):
        positions = self._make_positions(n=150, win_pct=0.70)
        result = _build_wallet_stats("0xABC", positions)
        assert math.isfinite(result["sharpe_ratio"])

    def test_mixed_pnl_gives_realistic_win_rate(self):
        """Ensure that mixed positive/negative PnL produces realistic win rates."""
        positions = self._make_positions(n=200, win_pct=0.60)
        result = _build_wallet_stats("0xABC", positions)
        assert result is not None
        assert result["win_rate"] < 1.0  # Must NOT be 100%
        assert result["win_rate"] > 0.5
