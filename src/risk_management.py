"""
Risk Management Module
======================
Calculates optimal USDC position sizes for each copy trade using:
  - Kelly Criterion (full / half / quarter Kelly)
  - Maximum portfolio risk cap of 5% per trade
  - Slippage adjustment (1–3%)

Input : JSON produced by the Wallet Discovery module.
Output: JSON mapping (wallet, market) → exact USDC amount to trade.

Design is deliberately modular: any other bot can ``import`` and call
``calculate_position_sizes`` directly.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
MAX_PORTFOLIO_RISK: float = 0.05    # 5% of portfolio per trade
DEFAULT_SLIPPAGE: float = 0.02      # 2% default slippage estimate
MIN_SLIPPAGE: float = 0.01          # 1%
MAX_SLIPPAGE: float = 0.03          # 3%
KELLY_FRACTION_FULL: float = 1.0
KELLY_FRACTION_HALF: float = 0.5
KELLY_FRACTION_QUARTER: float = 0.25
DEFAULT_KELLY: str = "half"         # "full" | "half" | "quarter"
MIN_TRADE_SIZE_USDC: float = 1.0    # never trade less than $1


# ---------------------------------------------------------------------------
# Core Kelly computation
# ---------------------------------------------------------------------------
def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Classic Kelly Criterion.

    f* = (p * b - q) / b

    where:
      p  = probability of winning  (win_rate)
      q  = probability of losing   (1 - win_rate)
      b  = win/loss ratio           (avg_win / avg_loss)

    Clamped to [0, 1] to avoid negative or >100% allocations.
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    q = 1.0 - win_rate
    f = (win_rate * b - q) / b
    return max(0.0, min(1.0, f))


def adjusted_kelly(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    kelly_mode: str = DEFAULT_KELLY,
) -> float:
    """
    Return the scaled Kelly fraction according to *kelly_mode*.

    Modes:
      ``"full"``    → full Kelly (aggressive, rarely recommended)
      ``"half"``    → half Kelly (balanced)
      ``"quarter"`` → quarter Kelly (conservative)
    """
    scale_map = {
        "full": KELLY_FRACTION_FULL,
        "half": KELLY_FRACTION_HALF,
        "quarter": KELLY_FRACTION_QUARTER,
    }
    scale = scale_map.get(kelly_mode, KELLY_FRACTION_HALF)
    raw = kelly_fraction(win_rate, avg_win, avg_loss)
    scaled = raw * scale
    logger.debug(
        "Kelly raw=%.4f  scale=%s (%.2f)  adjusted=%.4f",
        raw, kelly_mode, scale, scaled,
    )
    return scaled


# ---------------------------------------------------------------------------
# Slippage helpers
# ---------------------------------------------------------------------------
def estimate_slippage(position_size_usdc: float) -> float:
    """
    Heuristic slippage estimate based on position size.

    - Small positions  (<$500)  → 1% (thin order book, but small impact)
    - Medium positions ($500–$5k) → 2%
    - Large positions  (>$5k)  → 3%
    """
    if position_size_usdc < 500:
        return MIN_SLIPPAGE
    if position_size_usdc < 5_000:
        return DEFAULT_SLIPPAGE
    return MAX_SLIPPAGE


def apply_slippage(size_usdc: float, slippage: float) -> float:
    """Return the effective position size after slippage is deducted."""
    effective = size_usdc * (1.0 - slippage)
    logger.debug(
        "Slippage %.1f%%: $%.2f → $%.2f", slippage * 100, size_usdc, effective
    )
    return effective


# ---------------------------------------------------------------------------
# Per-trade sizing
# ---------------------------------------------------------------------------
def size_single_trade(
    portfolio_value_usdc: float,
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    kelly_mode: str = DEFAULT_KELLY,
    override_slippage: float | None = None,
) -> dict[str, Any]:
    """
    Compute the recommended USDC trade size for a single trade.

    Parameters
    ----------
    portfolio_value_usdc : float
        Current total portfolio value in USDC.
    win_rate : float
        Fraction of trades won (0–1).
    avg_win_pct : float
        Average gain as a fraction of position (e.g. 0.80 = 80%).
    avg_loss_pct : float
        Average loss as a fraction of position (e.g. 0.40 = 40%).
    kelly_mode : str
        "full" | "half" | "quarter"
    override_slippage : float | None
        Explicit slippage override; auto-estimated when None.

    Returns
    -------
    dict with keys: kelly_fraction, risk_capped_size, slippage_pct,
    effective_size, clamped_to_min
    """
    # 1. Compute Kelly-scaled allocation fraction
    frac = adjusted_kelly(win_rate, avg_win_pct, avg_loss_pct, kelly_mode)
    kelly_size = portfolio_value_usdc * frac

    # 2. Cap at max portfolio risk
    max_risk_size = portfolio_value_usdc * MAX_PORTFOLIO_RISK
    risk_capped = min(kelly_size, max_risk_size)

    if kelly_size > max_risk_size:
        logger.info(
            "Kelly size $%.2f exceeds 5%% cap ($%.2f); capped.",
            kelly_size, max_risk_size,
        )

    # 3. Slippage adjustment
    slippage = override_slippage if override_slippage is not None else estimate_slippage(risk_capped)
    effective = apply_slippage(risk_capped, slippage)

    # 4. Enforce minimum trade size
    clamped = effective < MIN_TRADE_SIZE_USDC
    final_size = max(effective, MIN_TRADE_SIZE_USDC) if effective > 0 else 0.0

    return {
        "kelly_fraction": round(frac, 6),
        "kelly_mode": kelly_mode,
        "raw_kelly_size_usdc": round(kelly_size, 4),
        "risk_capped_size_usdc": round(risk_capped, 4),
        "slippage_pct": round(slippage * 100, 2),
        "effective_size_usdc": round(final_size, 4),
        "clamped_to_min": clamped,
    }


# ---------------------------------------------------------------------------
# Batch sizing from Wallet Discovery output
# ---------------------------------------------------------------------------
def calculate_position_sizes(
    wallet_stats: list[dict[str, Any]],
    portfolio_value_usdc: float,
    kelly_mode: str = DEFAULT_KELLY,
) -> list[dict[str, Any]]:
    """
    Take the output of ``wallet_discovery.discover_top_traders`` and return
    a list of trade-size recommendations, one per wallet.

    Parameters
    ----------
    wallet_stats : list[dict]
        JSON output from the Wallet Discovery module.
    portfolio_value_usdc : float
        Current total USDC portfolio value.
    kelly_mode : str
        Kelly variant to apply globally ("full" | "half" | "quarter").

    Returns
    -------
    list of dicts ready for the Trade Execution module, each containing:
      wallet, market_focus, win_rate, trade_count, recommended_size_usdc, …
    """
    if not wallet_stats:
        logger.warning("No wallet stats provided – returning empty list")
        return []

    results: list[dict[str, Any]] = []

    for stat in wallet_stats:
        wallet = stat.get("wallet", "unknown")
        win_rate = float(stat.get("win_rate", 0.0))
        avg_size = float(stat.get("avg_position_size_usdc", 0.0))

        # Derive rough win/loss averages from the Sharpe and win_rate
        # (exact per-trade PnL streams are not carried in the summary JSON)
        sharpe = float(stat.get("sharpe_ratio", 0.0))
        # Heuristic: avg_win ≈ avg_size * (1 + |sharpe| * 0.1)
        #            avg_loss ≈ avg_size * (1 - win_rate)
        avg_win_pct = min(0.9, max(0.05, abs(sharpe) * 0.1 + 0.2))
        avg_loss_pct = min(0.9, max(0.05, 1.0 - win_rate))

        sizing = size_single_trade(
            portfolio_value_usdc=portfolio_value_usdc,
            win_rate=win_rate,
            avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct,
            kelly_mode=kelly_mode,
        )

        result = {
            "wallet": wallet,
            "market_focus": stat.get("market_focus", "other"),
            "win_rate": win_rate,
            "trade_count": stat.get("trade_count", 0),
            "sharpe_ratio": stat.get("sharpe_ratio", 0.0),
            "avg_position_size_usdc": avg_size,
            **sizing,
        }
        logger.info(
            "Wallet %s  win_rate=%.1f%%  kelly=%s  recommended=$%.2f",
            wallet[:10] + "…",
            win_rate * 100,
            kelly_mode,
            sizing["effective_size_usdc"],
        )
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    # Example: feed the output of wallet_discovery directly
    sample_wallets = [
        {
            "wallet": "0xABC123",
            "win_rate": 0.72,
            "avg_position_size_usdc": 1500.0,
            "sharpe_ratio": 1.8,
            "market_focus": "crypto",
            "trade_count": 210,
        }
    ]
    portfolio = 50_000.0  # $50k portfolio
    sizes = calculate_position_sizes(sample_wallets, portfolio, kelly_mode="half")
    print(json.dumps(sizes, indent=2))
