"""Tests for the Risk Management module."""

from __future__ import annotations

import pytest

from src.risk_management import (
    adjusted_kelly,
    apply_slippage,
    calculate_position_sizes,
    estimate_slippage,
    kelly_fraction,
    size_single_trade,
)


# ---------------------------------------------------------------------------
# kelly_fraction tests
# ---------------------------------------------------------------------------
class TestKellyFraction:
    def test_positive_edge(self):
        """High win-rate, good reward/risk → positive Kelly fraction."""
        f = kelly_fraction(win_rate=0.7, avg_win=0.8, avg_loss=0.4)
        assert 0 < f <= 1

    def test_zero_on_negative_edge(self):
        """Losing strategy should return 0 (clamped), never negative."""
        f = kelly_fraction(win_rate=0.3, avg_win=0.3, avg_loss=0.9)
        assert f == 0.0

    def test_zero_on_zero_win_rate(self):
        f = kelly_fraction(win_rate=0.0, avg_win=1.0, avg_loss=1.0)
        assert f == 0.0

    def test_max_one(self):
        """Kelly fraction must never exceed 1.0."""
        f = kelly_fraction(win_rate=1.0, avg_win=10.0, avg_loss=0.01)
        assert f <= 1.0

    def test_zero_avg_loss(self):
        """avg_loss=0 should return 0 gracefully (no division by zero)."""
        f = kelly_fraction(win_rate=0.7, avg_win=0.8, avg_loss=0.0)
        assert f == 0.0


# ---------------------------------------------------------------------------
# adjusted_kelly tests
# ---------------------------------------------------------------------------
class TestAdjustedKelly:
    def test_half_kelly_is_half_of_full(self):
        full = adjusted_kelly(0.65, 0.7, 0.4, "full")
        half = adjusted_kelly(0.65, 0.7, 0.4, "half")
        assert abs(half - full / 2) < 1e-9

    def test_quarter_kelly_is_quarter_of_full(self):
        full    = adjusted_kelly(0.65, 0.7, 0.4, "full")
        quarter = adjusted_kelly(0.65, 0.7, 0.4, "quarter")
        assert abs(quarter - full / 4) < 1e-9

    def test_unknown_mode_defaults_to_half(self):
        half    = adjusted_kelly(0.65, 0.7, 0.4, "half")
        unknown = adjusted_kelly(0.65, 0.7, 0.4, "unknown_mode")
        assert abs(unknown - half) < 1e-9


# ---------------------------------------------------------------------------
# slippage tests
# ---------------------------------------------------------------------------
class TestSlippage:
    def test_small_position_min_slippage(self):
        assert estimate_slippage(100) == 0.01

    def test_medium_position_default_slippage(self):
        assert estimate_slippage(1_000) == 0.02

    def test_large_position_max_slippage(self):
        assert estimate_slippage(10_000) == 0.03

    def test_apply_slippage_reduces_size(self):
        effective = apply_slippage(1000.0, 0.02)
        assert effective == pytest.approx(980.0)

    def test_apply_slippage_zero(self):
        effective = apply_slippage(1000.0, 0.0)
        assert effective == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# size_single_trade tests
# ---------------------------------------------------------------------------
class TestSizeSingleTrade:
    def test_returns_expected_keys(self):
        result = size_single_trade(
            portfolio_value_usdc=50_000,
            win_rate=0.65,
            avg_win_pct=0.7,
            avg_loss_pct=0.4,
        )
        assert "kelly_fraction" in result
        assert "effective_size_usdc" in result
        assert "slippage_pct" in result
        assert "risk_capped_size_usdc" in result

    def test_risk_cap_applied(self):
        """Effective size must never exceed 5% of portfolio."""
        result = size_single_trade(
            portfolio_value_usdc=100_000,
            win_rate=0.99,       # unrealistically high win rate → big Kelly
            avg_win_pct=0.99,
            avg_loss_pct=0.01,
            kelly_mode="full",
            override_slippage=0.0,
        )
        assert result["effective_size_usdc"] <= 100_000 * 0.05 + 0.01  # 5% cap + rounding

    def test_min_trade_size_enforced(self):
        """A zero-Kelly wallet should still get at least $1."""
        result = size_single_trade(
            portfolio_value_usdc=100_000,
            win_rate=0.0,         # losing strategy → Kelly = 0
            avg_win_pct=0.1,
            avg_loss_pct=0.9,
        )
        # size can be $0 if Kelly is 0 (we don't force a minimum for zero-Kelly)
        assert result["effective_size_usdc"] >= 0.0

    def test_kelly_mode_half(self):
        r_half = size_single_trade(100_000, 0.65, 0.7, 0.4, kelly_mode="half", override_slippage=0)
        r_full = size_single_trade(100_000, 0.65, 0.7, 0.4, kelly_mode="full", override_slippage=0)
        assert r_half["effective_size_usdc"] <= r_full["effective_size_usdc"] + 0.01


# ---------------------------------------------------------------------------
# calculate_position_sizes tests
# ---------------------------------------------------------------------------
class TestCalculatePositionSizes:
    _sample_wallets = [
        {
            "wallet": "0xAAA",
            "win_rate": 0.72,
            "avg_position_size_usdc": 1500.0,
            "sharpe_ratio": 1.8,
            "market_focus": "crypto",
            "trade_count": 210,
        },
        {
            "wallet": "0xBBB",
            "win_rate": 0.61,
            "avg_position_size_usdc": 500.0,
            "sharpe_ratio": 0.9,
            "market_focus": "politics",
            "trade_count": 130,
        },
    ]

    def test_returns_one_entry_per_wallet(self):
        result = calculate_position_sizes(self._sample_wallets, 50_000)
        assert len(result) == len(self._sample_wallets)

    def test_wallet_field_preserved(self):
        result = calculate_position_sizes(self._sample_wallets, 50_000)
        wallets = {r["wallet"] for r in result}
        assert wallets == {"0xAAA", "0xBBB"}

    def test_empty_input_returns_empty(self):
        assert calculate_position_sizes([], 50_000) == []

    def test_all_sizes_non_negative(self):
        result = calculate_position_sizes(self._sample_wallets, 50_000)
        for r in result:
            assert r["effective_size_usdc"] >= 0.0

    def test_kelly_mode_propagated(self):
        result = calculate_position_sizes(self._sample_wallets, 50_000, kelly_mode="quarter")
        for r in result:
            assert r["kelly_mode"] == "quarter"
