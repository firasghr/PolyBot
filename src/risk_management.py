"""
Risk Management Module
======================
Calculates optimal USDC position sizes for each copy trade using:
  - Kelly Criterion (full / half / quarter Kelly)
  - Maximum portfolio risk cap of 5% per trade
  - Slippage adjustment (1–3%)

Also provides a PortfolioRiskManager to reject trades if:
  - Max open trades (10) exceeded
  - Max category exposure (25% of portfolio) exceeded

Input : JSON produced by the Wallet Discovery module.
Output: JSON mapping (wallet, market) → exact USDC amount to trade.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

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

MAX_OPEN_TRADES: int = 10
MAX_CATEGORY_EXPOSURE: float = 0.25 # 25% max per category


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
    """
    scale_map = {
        "full": KELLY_FRACTION_FULL,
        "half": KELLY_FRACTION_HALF,
        "quarter": KELLY_FRACTION_QUARTER,
    }
    scale = scale_map.get(kelly_mode, KELLY_FRACTION_HALF)
    raw = kelly_fraction(win_rate, avg_win, avg_loss)
    scaled = raw * scale
    return scaled


# ---------------------------------------------------------------------------
# Slippage helpers
# ---------------------------------------------------------------------------
def estimate_slippage(size_usdc: float) -> float:
    """
    Estimate slippage based on trade size.

    Returns:
      - MIN_SLIPPAGE (1%) for small trades (< $500)
      - MAX_SLIPPAGE (3%) for large trades (>= $5,000)
      - DEFAULT_SLIPPAGE (2%) for medium trades
    """
    if size_usdc < 500:
        return MIN_SLIPPAGE
    if size_usdc >= 5_000:
        return MAX_SLIPPAGE
    return DEFAULT_SLIPPAGE


def apply_slippage(size_usdc: float, slippage: float) -> float:
    """
    Return the effective trade size after deducting slippage cost.

    effective = size * (1 - slippage)
    """
    return size_usdc * (1.0 - slippage)


def calculate_exact_slippage(
    desired_size_usdc: float, 
    orderbook: dict[str, Any], 
    side: str = "BUY"
) -> tuple[float, float, str]:
    """
    Traverse the orderbook depth to calculate exact weighted-average fill price.
    Returns: (effective_size_usdc, slippage_pct, reason_if_aborted)
    """
    if not orderbook or desired_size_usdc <= 0:
        return 0.0, 0.0, "Missing orderbook or zero size"
        
    # If buying, we lift the asks. If selling, we hit the bids.
    levels = orderbook.get("asks" if side == "BUY" else "bids", [])
    if not levels:
        return 0.0, 0.0, "Empty orderbook side"

    # Best price is the top of the book
    try:
        best_price = float(levels[0].get("price", 0))
    except (ValueError, IndexError):
        return 0.0, 0.0, "Invalid orderbook price format"
        
    if best_price <= 0:
        return 0.0, 0.0, "Invalid best price"

    remaining_usdc = desired_size_usdc
    total_shares_filled = 0.0
    total_usdc_spent = 0.0
    
    for level in levels:
        try:
            p = float(level.get("price", 0))
            s = float(level.get("size", 0))  # shares available at this level
        except ValueError:
            continue
            
        level_usdc_capacity = p * s
        
        if remaining_usdc <= level_usdc_capacity:
            # We can finish our order at this level
            shares_bought = remaining_usdc / p
            total_shares_filled += shares_bought
            total_usdc_spent += remaining_usdc
            remaining_usdc = 0
            break
        else:
            # Consume the whole level
            total_shares_filled += s
            total_usdc_spent += level_usdc_capacity
            remaining_usdc -= level_usdc_capacity
            
    if remaining_usdc > 0:
        return 0.0, 0.0, "Insufficient liquidity to fill order"
        
    if total_shares_filled <= 0:
        return 0.0, 0.0, "Zero shares filled"

    average_fill_price = total_usdc_spent / total_shares_filled
    
    # Slippage is how much worse our average fill is compared to the best price
    slippage_pct = abs(average_fill_price - best_price) / best_price
    
    if slippage_pct > 0.05:
        return 0.0, slippage_pct, f"Slippage > 5% ({slippage_pct*100:.2f}%)"
        
    return total_usdc_spent, slippage_pct, ""


# ---------------------------------------------------------------------------
# Per-trade sizing
# ---------------------------------------------------------------------------
def size_single_trade(
    portfolio_value_usdc: float,
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    kelly_mode: str = DEFAULT_KELLY,
    orderbook: dict[str, Any] | None = None,
    side: str = "BUY",
    override_slippage: float | None = None,
) -> dict[str, Any]:
    """Compute the recommended USDC trade size via Kelly and slippage adjustment.

    Parameters
    ----------
    override_slippage : float | None
        When provided, use this exact slippage fraction instead of estimating
        from the orderbook.  Pass ``0.0`` to disable slippage entirely.
    """
    # 1. Compute Kelly-scaled allocation fraction
    frac = adjusted_kelly(win_rate, avg_win_pct, avg_loss_pct, kelly_mode)
    kelly_size = portfolio_value_usdc * frac

    # 2. Cap at max portfolio risk
    max_risk_size = portfolio_value_usdc * MAX_PORTFOLIO_RISK
    risk_capped = min(kelly_size, max_risk_size)

    # 3. Slippage calculation
    slippage = 0.0
    effective = risk_capped
    abort_reason = ""

    if override_slippage is not None:
        # Caller supplied an explicit slippage fraction (including 0.0 = no slippage)
        slippage = float(override_slippage)
        effective = apply_slippage(risk_capped, slippage)
    elif orderbook:
        effective, slippage, abort_reason = calculate_exact_slippage(
            desired_size_usdc=risk_capped,
            orderbook=orderbook,
            side=side,
        )
    else:
        # Estimate slippage from trade size when no orderbook is available
        slippage = estimate_slippage(risk_capped)
        effective = apply_slippage(risk_capped, slippage)

    # 4. Enforce minimum trade size
    clamped = effective < MIN_TRADE_SIZE_USDC
    final_size = max(effective, MIN_TRADE_SIZE_USDC) if effective > 0 else 0.0

    if abort_reason:
        final_size = 0.0

    return {
        "kelly_fraction": round(frac, 6),
        "kelly_mode": kelly_mode,
        "raw_kelly_size_usdc": round(kelly_size, 4),
        "risk_capped_size_usdc": round(risk_capped, 4),
        "slippage_pct": round(slippage * 100, 2),
        "effective_size_usdc": round(final_size, 4),
        "clamped_to_min": clamped,
        "abort_reason": abort_reason,
    }


# ---------------------------------------------------------------------------
# Batch sizing from Wallet Discovery output
# ---------------------------------------------------------------------------
def calculate_position_sizes(
    wallet_stats: list[dict[str, Any]],
    portfolio_value_usdc: float,
    kelly_mode: str = DEFAULT_KELLY,
) -> list[dict[str, Any]]:
    """Take the output of wallet_discovery and return trade-size recommendations."""
    if not wallet_stats:
        return []

    results: list[dict[str, Any]] = []

    for stat in wallet_stats:
        wallet = stat.get("wallet", "unknown")
        win_rate = float(stat.get("win_rate", 0.0))
        avg_size = float(stat.get("avg_position_size_usdc", 0.0))
        
        # In the new wallet_discovery, we have exact avg_win and avg_loss.
        # But we need them as percentages of position size to plug into Kelly.
        avg_win_usdc = float(stat.get("avg_win_usdc", 0.0))
        avg_loss_usdc = float(stat.get("avg_loss_usdc", 0.0))
        
        if avg_size > 0:
            avg_win_pct = avg_win_usdc / avg_size
            avg_loss_pct = avg_loss_usdc / avg_size
        else:
            # Fallback if somehow avg_size is 0
            sharpe = float(stat.get("sharpe_ratio", 0.0))
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
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Portfolio Risk Manager
# ---------------------------------------------------------------------------
class PortfolioRiskManager:
    """Enforces portfolio-level rules (max open trades, category limits)."""
    
    def __init__(
        self,
        max_open_trades: int = MAX_OPEN_TRADES,
        max_category_exposure: float = MAX_CATEGORY_EXPOSURE,
    ) -> None:
        self.max_open_trades = max_open_trades
        self.max_category_exposure = max_category_exposure

    def can_open_trade(
        self,
        new_trade_size: float,
        category: str,
        total_portfolio_value: float,
        open_trades: Iterable[Any],
    ) -> tuple[bool, str]:
        """
        Check if a prospective trade violates portfolio risk limits.
        
        Returns (True, "") if allowed, or (False, "reason") if rejected.
        """
        open_list = list(open_trades)
        
        # 1. Max Open Trades Check
        if len(open_list) >= self.max_open_trades:
            return False, f"Max open trades ({self.max_open_trades}) reached."
            
        # 2. Category Exposure Check
        category_exposure = new_trade_size
        for t in open_list:
            # Look up trade category (assume object has .category or we derive it)
            t_cat = getattr(t, "category", "other")
            if t_cat == category:
                category_exposure += getattr(t, "size_usdc", 0.0)
                
        max_allowed_for_cat = total_portfolio_value * self.max_category_exposure
        if category_exposure > max_allowed_for_cat:
            return False, f"Risk limit: {category} exposure (${category_exposure:.2f}) exceeds {self.max_category_exposure*100}% max (${max_allowed_for_cat:.2f})."
            
        return True, ""
