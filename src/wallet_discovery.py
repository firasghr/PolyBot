"""
Wallet Discovery Module
=======================
Scans Polymarket wallets on Polygon to identify the top 10 directional traders
suitable for copy trading.

Criteria:
  - ≥100 historical trades
  - Win rate ≥60%
  - Large directional positions in specific markets
  - Avoids leaderboard / top-visible wallets

For each qualifying wallet the module computes:
  - Average win rate
  - Average position size (USDC)
  - Market focus (crypto, politics, sports, …)
  - Historical Sharpe ratio

Output: JSON list ready to feed the Risk Management / Trade Execution modules.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import defaultdict
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
POLYMARKET_CLOB_API = os.getenv("POLYMARKET_CLOB_API", "https://clob.polymarket.com")
POLYMARKET_GAMMA_API = os.getenv(
    "POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"
)
POLYGONSCAN_API = os.getenv("POLYGONSCAN_API", "https://api.polygonscan.com/api")
POLYGONSCAN_API_KEY = os.getenv("POLYGONSCAN_API_KEY", "")

MIN_TRADES: int = int(os.getenv("MIN_TRADES", "100"))
MIN_WIN_RATE: float = float(os.getenv("MIN_WIN_RATE", "0.60"))
TOP_N: int = int(os.getenv("TOP_N", "10"))
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "8"))

# ---------------------------------------------------------------------------
# Market-focus keyword mappings
# ---------------------------------------------------------------------------
MARKET_KEYWORDS: dict[str, list[str]] = {
    "crypto": ["bitcoin", "btc", "eth", "ethereum", "crypto", "defi", "nft", "solana"],
    "politics": ["election", "president", "senate", "congress", "vote", "government"],
    "sports": ["nba", "nfl", "soccer", "football", "basketball", "championship", "world cup"],
    "finance": ["stocks", "fed", "interest rate", "inflation", "gdp", "earnings"],
}


def _classify_market(question: str) -> str:
    """Return the primary topic category for a market question."""
    q = question.lower()
    for category, keywords in MARKET_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, Any] | None = None,
    retries: int = 3,
    backoff: float = 1.0,
) -> Any:
    """Perform a GET request with exponential-backoff retries."""
    for attempt in range(1, retries + 1):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                logger.error("Failed %s after %d attempts: %s", url, retries, exc)
                return None
            wait = backoff * (2 ** (attempt - 1))
            logger.warning("Attempt %d/%d failed (%s). Retrying in %.1fs", attempt, retries, exc, wait)
            await asyncio.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Polymarket API layer
# ---------------------------------------------------------------------------
async def fetch_active_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch the list of currently active Polymarket markets."""
    logger.info("Fetching active markets from Gamma API …")
    data = await _get_json(
        session,
        f"{POLYMARKET_GAMMA_API}/markets",
        params={"active": "true", "limit": 500, "offset": 0},
    )
    if data is None:
        return []
    markets = data if isinstance(data, list) else data.get("markets", [])
    logger.info("Retrieved %d active markets", len(markets))
    return markets


async def fetch_market_trades(
    session: aiohttp.ClientSession,
    condition_id: str,
    limit: int = 500,
) -> list[dict]:
    """Fetch recent trades for a specific market condition ID."""
    data = await _get_json(
        session,
        f"{POLYMARKET_CLOB_API}/trades",
        params={"market": condition_id, "limit": limit},
    )
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


async def fetch_wallet_positions(
    session: aiohttp.ClientSession,
    wallet: str,
) -> list[dict]:
    """Fetch all historical positions for a wallet via the CLOB API."""
    data = await _get_json(
        session,
        f"{POLYMARKET_CLOB_API}/positions",
        params={"user": wallet, "limit": 1000},
    )
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


async def fetch_leaderboard_wallets(
    session: aiohttp.ClientSession,
) -> set[str]:
    """
    Return the set of wallet addresses that appear on Polymarket public
    leaderboards so we can *exclude* them from copy-trading candidates.
    """
    data = await _get_json(
        session,
        f"{POLYMARKET_GAMMA_API}/leaderboard",
        params={"limit": 100},
    )
    if data is None:
        return set()
    entries = data if isinstance(data, list) else data.get("data", [])
    wallets = {
        str(e.get("address", e.get("wallet", ""))).lower()
        for e in entries
        if e.get("address") or e.get("wallet")
    }
    logger.info("Leaderboard contains %d wallets (will be excluded)", len(wallets))
    return wallets


# ---------------------------------------------------------------------------
# Wallet statistics computation
# ---------------------------------------------------------------------------
def _compute_sharpe(pnl_series: list[float], risk_free: float = 0.0) -> float:
    """
    Compute the historical Sharpe ratio from a series of per-trade PnL values.

    Sharpe = (mean_return - risk_free) / std_return
    Returns 0.0 when std is 0 or the series is too short.
    """
    if len(pnl_series) < 2:
        return 0.0
    n = len(pnl_series)
    mean = sum(pnl_series) / n
    variance = sum((x - mean) ** 2 for x in pnl_series) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return (mean - risk_free) / std


def _build_wallet_stats(
    wallet: str,
    positions: list[dict],
) -> dict[str, Any] | None:
    """
    Derive per-wallet statistics from its raw position history.

    Returns None when the wallet does not meet minimum criteria.
    """
    if len(positions) < MIN_TRADES:
        return None

    pnl_series: list[float] = []
    wins = 0
    total_size = 0.0
    market_counts: dict[str, int] = defaultdict(int)

    for pos in positions:
        # Each position record is expected to contain:
        #   outcome: "YES" | "NO"
        #   size   : float  (USDC notional)
        #   pnl    : float  (realised PnL in USDC; negative = loss)
        #   market_question: str

        pnl = float(pos.get("pnl", pos.get("profitLoss", 0)))
        size = float(pos.get("size", pos.get("amount", 0)))
        question = str(pos.get("market_question", pos.get("title", "")))

        pnl_series.append(pnl)
        if pnl > 0:
            wins += 1
        total_size += size

        category = _classify_market(question)
        market_counts[category] += 1

    trade_count = len(positions)
    win_rate = wins / trade_count if trade_count else 0.0

    if win_rate < MIN_WIN_RATE:
        return None

    avg_size = total_size / trade_count if trade_count else 0.0
    sharpe = _compute_sharpe(pnl_series)
    primary_focus = max(market_counts, key=lambda k: market_counts[k]) if market_counts else "other"

    return {
        "wallet": wallet,
        "trade_count": trade_count,
        "win_rate": round(win_rate, 4),
        "avg_position_size_usdc": round(avg_size, 2),
        "market_focus": primary_focus,
        "market_distribution": dict(market_counts),
        "sharpe_ratio": round(sharpe, 4),
        "total_pnl_usdc": round(sum(pnl_series), 2),
    }


# ---------------------------------------------------------------------------
# Wallet collection
# ---------------------------------------------------------------------------
async def collect_candidate_wallets(
    session: aiohttp.ClientSession,
    markets: list[dict],
    leaderboard: set[str],
    semaphore: asyncio.Semaphore,
) -> set[str]:
    """
    Walk through recent trades on all markets and accumulate unique wallet
    addresses that are NOT on the leaderboard.

    Uses a semaphore to cap concurrent API calls.
    """

    async def _fetch(condition_id: str) -> list[str]:
        async with semaphore:
            trades = await fetch_market_trades(session, condition_id)
        return [
            str(t.get("maker", t.get("user", ""))).lower()
            for t in trades
            if t.get("maker") or t.get("user")
        ]

    tasks = [_fetch(m.get("conditionId", m.get("id", ""))) for m in markets if m.get("conditionId") or m.get("id")]
    results = await asyncio.gather(*tasks)

    candidates: set[str] = set()
    for wallet_list in results:
        for w in wallet_list:
            if w and w not in leaderboard:
                candidates.add(w)

    logger.info("Discovered %d candidate wallets (leaderboard excluded)", len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------
async def discover_top_traders(
    top_n: int = TOP_N,
) -> list[dict[str, Any]]:
    """
    Full pipeline: scan Polymarket → collect wallets → score each wallet →
    return the top ``top_n`` directional traders sorted by Sharpe ratio.

    Returns a JSON-serialisable list of wallet stat dicts.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession() as session:
        # Step 1: gather context (markets + leaderboard) concurrently
        markets_task = asyncio.create_task(fetch_active_markets(session))
        leaderboard_task = asyncio.create_task(fetch_leaderboard_wallets(session))
        markets, leaderboard = await asyncio.gather(markets_task, leaderboard_task)

        if not markets:
            logger.warning("No markets returned – returning empty result")
            return []

        # Step 2: collect candidate wallet addresses from trade history
        candidates = await collect_candidate_wallets(session, markets, leaderboard, semaphore)

        if not candidates:
            logger.warning("No candidate wallets found")
            return []

        # Step 3: fetch each wallet's full position history concurrently
        logger.info("Fetching position history for %d candidates …", len(candidates))

        async def _positions_for(wallet: str) -> dict[str, Any] | None:
            async with semaphore:
                positions = await fetch_wallet_positions(session, wallet)
            return _build_wallet_stats(wallet, positions)

        stat_tasks = [_positions_for(w) for w in candidates]
        all_stats = await asyncio.gather(*stat_tasks)

    # Step 4: filter out wallets that didn't pass criteria (returned None)
    qualified = [s for s in all_stats if s is not None]
    logger.info("%d wallets passed all filters", len(qualified))

    # Step 5: rank by Sharpe ratio (primary) then win rate (secondary)
    qualified.sort(key=lambda x: (x["sharpe_ratio"], x["win_rate"]), reverse=True)
    top_traders = qualified[:top_n]

    logger.info("Top %d traders selected", len(top_traders))
    return top_traders


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    traders = asyncio.run(discover_top_traders())
    print(json.dumps(traders, indent=2))
