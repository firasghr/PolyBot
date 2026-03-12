"""
Wallet Discovery Module
=======================
Scans Polymarket wallets on Polygon to identify the top 20 directional traders
suitable for copy trading.

Criteria:
  - ≥100 historical trades
  - Win rate ≥60%
  - Large directional positions in specific markets

For each qualifying wallet the module computes:
  - Average win rate
  - Average position size (USDC)
  - Average win / loss amounts
  - Market focus (crypto, politics, sports, …)
  - Historical Sharpe ratio
  - Composite score for ranking

Output: JSON list ready to feed the Risk Management / Trade Execution modules.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections import defaultdict
from typing import Any

import aiohttp

from .market_cache import MarketCache

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
POLYMARKET_GAMMA_API = os.getenv(
    "POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"
)
POLYMARKET_DATA_API = os.getenv(
    "POLYMARKET_DATA_API", "https://data-api.polymarket.com"
)

MIN_TRADES: int = int(os.getenv("MIN_TRADES", "100"))
MIN_WIN_RATE: float = float(os.getenv("MIN_WIN_RATE", "0.55"))
TOP_N: int = int(os.getenv("TOP_N", "20"))
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))

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
    retries: int = 5,
    backoff: float = 1.0,
) -> Any:
    """Perform a GET request with exponential-backoff retries and 429 handling."""
    for attempt in range(1, retries + 1):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else backoff * (2 ** (attempt - 1))
                    wait = min(wait, 30.0)
                    if attempt < retries:
                        logger.debug("Rate-limited on %s, waiting %.1fs (attempt %d/%d)", url, wait, attempt, retries)
                        await asyncio.sleep(wait)
                        continue
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
    """Fetch the list of currently active Polymarket markets (top 50 by volume)."""
    logger.info("Fetching active markets from Gamma API …")
    data = await _get_json(
        session,
        f"{POLYMARKET_GAMMA_API}/markets",
        params={"active": "true", "closed": "false", "limit": 100, "offset": 0, "order": "volume24hr", "ascending": "false"},
    )
    if data is None:
        return []
    markets = data if isinstance(data, list) else data.get("markets", [])
    markets = markets[:50]
    logger.info("Selected %d active markets for scanning", len(markets))
    return markets


async def fetch_market_trades(
    session: aiohttp.ClientSession,
    condition_id: str,
    limit: int = 500,
) -> list[dict]:
    """Fetch recent trades for a specific market via the public Data API."""
    data = await _get_json(
        session,
        f"{POLYMARKET_DATA_API}/trades",
        params={"market": condition_id, "limit": limit},
    )
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


async def fetch_wallet_positions(
    session: aiohttp.ClientSession,
    wallet: str,
    market_cache: MarketCache | None = None,
) -> list[dict]:
    """
    Fetch all historical activity for a wallet via the public Data API.

    Uses MarketCache to compute real PnL based on market resolution:
      - Resolved markets: PnL = size*(1-price) for wins, -size*price for losses
      - Unresolved markets: PnL = size*(current_price - entry_price) (unrealised)
    """
    all_entries = []
    offset = 0
    max_history = 4000
    
    while offset < max_history:
        data = await _get_json(
            session,
            f"{POLYMARKET_DATA_API}/activity",
            params={"user": wallet, "limit": 1000, "offset": offset},
        )
        if data is None:
            break
            
        entries = data if isinstance(data, list) else data.get("data", [])
        if not entries:
            break
            
        all_entries.extend(entries)
        if len(entries) < 1000:
            break
        offset += 1000

    raw_trade_count = len(all_entries)
    entries = all_entries

    # Sort entries earliest first to build the ledger correctly
    entries.sort(key=lambda x: int(x.get("timestamp", 0) or 0))

    # Identify missing markets and resolve them if possible
    if market_cache:
        distinct_cids = list({e.get("conditionId") for e in entries if e.get("conditionId")})
        await market_cache.resolve_missing(distinct_cids)

    positions = []
    
    # Ledger: (condition_id, outcome) -> {'shares': float, 'invested_usdc': float, 'market_question': str}
    ledger = defaultdict(lambda: {'shares': 0.0, 'invested_usdc': 0.0, 'market_question': ""})

    for e in entries:
        if e.get("type") and e["type"] != "TRADE":
            continue

        size = float(e.get("usdcSize", e.get("size", 0)))
        price = float(e.get("price", 0.5))
        shares = size / price if price > 0 else 0.0
        
        condition_id = e.get("conditionId", "")
        outcome = e.get("outcome", "")          # e.g. "Yes", "No", "Up", "Down"
        side = e.get("side", "BUY").upper()
        title = e.get("title", "")
        
        if not condition_id or not outcome:
            continue
            
        key = (condition_id, outcome)
        pos = ledger[key]
        pos['market_question'] = title or pos.get('market_question', '')
        
        if side == "BUY":
            pos['shares'] += shares
            pos['invested_usdc'] += size
        elif side == "SELL":
            # Early exit: realize PnL on the shares sold
            if pos['shares'] > 0:
                # Average entry price for the remaining shares
                avg_entry_price = pos['invested_usdc'] / pos['shares']
                
                # Shares might be slightly off due to float math, cap at available
                shares_sold = min(shares, pos['shares'])
                
                # Realised PnL = exit_value - cost_basis
                cost_basis = shares_sold * avg_entry_price
                exit_value = shares_sold * price
                realised_pnl = exit_value - cost_basis
                
                pos['shares'] -= shares_sold
                pos['invested_usdc'] -= cost_basis
                
                positions.append({
                    "pnl": realised_pnl,
                    "size": exit_value,
                    "price": price,
                    "outcome": outcome,
                    "condition_id": condition_id,
                    "side": side,
                    "is_resolved": True,
                    "market_question": title,
                })

    # Close out remaining open positions based on market resolution
    for (condition_id, outcome), pos in ledger.items():
        if pos['shares'] <= 1e-6:
            continue
            
        avg_entry_price = pos['invested_usdc'] / pos['shares']
        pnl = 0.0
        is_resolved = False
        
        if market_cache:
            resolved, winning_outcome = market_cache.get_resolution(condition_id)

            if resolved and winning_outcome:
                is_resolved = True
                trader_won = (outcome.lower() == winning_outcome.lower())
                
                if trader_won:
                    # Payout is $1 per share; profit = payout - invested
                    payout = pos['shares'] * 1.0
                    pnl = payout - pos['invested_usdc']
                else:
                    # Shares expire worthless
                    pnl = -pos['invested_usdc']
            else:
                current_price = market_cache.get_current_price(condition_id, outcome)
                if current_price > 0:
                    current_value = pos['shares'] * current_price
                    pnl = current_value - pos['invested_usdc']
                
        positions.append({
            "pnl": pnl,
            "size": pos['invested_usdc'],
            "price": avg_entry_price,
            "outcome": outcome,
            "condition_id": condition_id,
            "side": "HOLD",
            "is_resolved": is_resolved,
            "market_question": pos['market_question'],
        })

    return positions, raw_trade_count


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


def _compute_wallet_score(stats: dict[str, Any]) -> float:
    """
    Composite score combining multiple performance metrics.

    score = 0.4 × win_rate + 0.3 × norm_sharpe + 0.2 × pnl_factor + 0.1 × trade_factor

    All components normalized to [0, 1].
    """
    win_rate = stats.get("win_rate", 0.0)
    sharpe = stats.get("sharpe_ratio", 0.0)
    total_pnl = stats.get("total_pnl_usdc", 0.0)
    trade_count = stats.get("trade_count", 0)

    norm_sharpe = max(0.0, min(1.0, sharpe / 3.0))
    pnl_factor = max(0.0, min(1.0, total_pnl / 10000.0)) if total_pnl > 0 else 0.0
    trade_factor = max(0.0, min(1.0, trade_count / 500.0))

    return round(
        0.4 * win_rate + 0.3 * norm_sharpe + 0.2 * pnl_factor + 0.1 * trade_factor,
        4,
    )


def _build_wallet_stats(
    wallet: str,
    positions: list[dict],
    raw_trade_count: int = 0,
    profile: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Derive per-wallet statistics from its raw position history.

    Returns None when the wallet does not meet minimum criteria.
    """
    if raw_trade_count < MIN_TRADES:
        return None

    pnl_series: list[float] = []
    wins = 0
    losses = 0
    total_size = 0.0
    total_win_pnl = 0.0
    total_loss_pnl = 0.0
    market_counts: dict[str, int] = defaultdict(int)

    for pos in positions:
        pnl = float(pos.get("pnl", pos.get("profitLoss", 0)))
        size = float(pos.get("size", pos.get("amount", 0)))
        question = str(pos.get("market_question", pos.get("title", "")))

        # Only add resolved trades to the PnL series for Sharpe ratio
        if pos.get("is_resolved", False):
            pnl_series.append(pnl)
            
            # Since early exits (SELL) can be functionally zero if bought and sold at exact same price
            # We enforce a small threshold to count as a win/loss
            if pnl > 0.01:
                wins += 1
                total_win_pnl += pnl
            elif pnl < -0.01:
                losses += 1
                total_loss_pnl += abs(pnl)

        total_size += size
        category = _classify_market(question)
        market_counts[category] += 1

    trade_count = len(positions)
    decided_trades = wins + losses
    win_rate = wins / decided_trades if decided_trades > 0 else 0.0

    # Need at least some resolved trades to meaningfully evaluate
    if decided_trades < max(10, MIN_TRADES // 5):
        return None

    if win_rate < MIN_WIN_RATE:
        return None
        
    # --- Impossible Stats bounds checks ---
    if win_rate > 0.90 and decided_trades > 100:
        logger.warning("Wallet %s rejected: Impossible stats (WR: %.2f, trades: %d)", wallet, win_rate, decided_trades)
        return None

    avg_size = total_size / trade_count if trade_count else 0.0
    avg_win = total_win_pnl / wins if wins > 0 else 0.0
    avg_loss = total_loss_pnl / losses if losses > 0 else 0.0
    sharpe = _compute_sharpe(pnl_series)
    
    # --- Market Maker / Liquidity Provider Filter ---
    # Disqualify traders with tiny profit edge relative to trade size (e.g. < 0.5%)
    # These are usually high-volume bots that a copy-trader cannot follow profitably.
    if avg_size > 0 and (avg_win / avg_size) < 0.005 and decided_trades > 50:
        logger.warning(
            "Wallet %s rejected: Market Maker detected (Edge: %.4f%%, Size: $%.0f)", 
            wallet, (avg_win/avg_size)*100, avg_size
        )
        return None

    if sharpe > 3.0 and decided_trades > 50:
        logger.warning("Wallet %s rejected: Impossible sharpe ratio (%.2f on %d trades)", wallet, sharpe, decided_trades)
        return None
        
    primary_focus = max(market_counts, key=lambda k: market_counts[k]) if market_counts else "other"

    prof = profile or {}
    stats = {
        "wallet": wallet,
        "name": prof.get("name", ""),
        "pseudonym": prof.get("pseudonym", ""),
        "profile_image": prof.get("profile_image", ""),
        "bio": prof.get("bio", ""),
        "trade_count": raw_trade_count,
        "decided_trades": decided_trades,
        "win_rate": round(win_rate, 4),
        "avg_position_size_usdc": round(avg_size, 2),
        "avg_win_usdc": round(avg_win, 2),
        "avg_loss_usdc": round(avg_loss, 2),
        "market_focus": primary_focus,
        "market_distribution": dict(market_counts),
        "sharpe_ratio": round(sharpe, 4),
        "total_pnl_usdc": round(sum(pnl_series), 2),
        "composite_score": 0.0,  # computed below
    }
    stats["composite_score"] = _compute_wallet_score(stats)
    return stats


# ---------------------------------------------------------------------------
# Wallet collection
# ---------------------------------------------------------------------------
async def collect_candidate_wallets(
    session: aiohttp.ClientSession,
    markets: list[dict],
    semaphore: asyncio.Semaphore,
) -> tuple[set[str], dict[str, dict[str, str]]]:
    """
    Walk through recent trades on all markets, count how often each wallet
    appears, and return the top 100 most-active addresses along with their
    profile metadata.

    Returns:
        (top_wallets, wallet_profiles) where wallet_profiles maps
        wallet address -> {name, pseudonym, profile_image, bio}
    """

    async def _fetch(condition_id: str) -> list[dict]:
        async with semaphore:
            trades = await fetch_market_trades(session, condition_id)
        return trades

    tasks = [_fetch(m.get("conditionId", m.get("id", ""))) for m in markets if m.get("conditionId") or m.get("id")]
    results = await asyncio.gather(*tasks)

    # Count how often each wallet appears across all markets and capture profiles
    wallet_counts: dict[str, int] = defaultdict(int)
    wallet_profiles: dict[str, dict[str, str]] = {}
    for trades in results:
        for t in trades:
            w = str(t.get("proxyWallet", t.get("maker", t.get("user", "")))).lower()
            if not w:
                continue
            wallet_counts[w] += 1
            if w not in wallet_profiles:
                wallet_profiles[w] = {
                    "name": t.get("name", ""),
                    "pseudonym": t.get("pseudonym", ""),
                    "profile_image": t.get("profileImage", t.get("profileImageOptimized", "")),
                    "bio": t.get("bio", ""),
                }

    logger.info("Found %d unique wallets across %d markets", len(wallet_counts), len(markets))

    # Only keep the top 100 most-active wallets to avoid rate-limiting
    sorted_wallets = sorted(wallet_counts.keys(), key=lambda w: wallet_counts[w], reverse=True)
    top_wallets = set(sorted_wallets[:100])

    logger.info("Selected top %d candidate wallets for analysis", len(top_wallets))
    return top_wallets, wallet_profiles


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------
async def discover_top_traders(
    market_cache: MarketCache | None = None,
    top_n: int = TOP_N,
) -> list[dict[str, Any]]:
    """
    Full pipeline: scan Polymarket → collect wallets → score each wallet →
    return the top ``top_n`` directional traders sorted by composite score.

    Parameters
    ----------
    market_cache : MarketCache | None
        Shared market cache for PnL resolution. If None, PnL estimation
        will be degraded (all unresolved trades get PnL=0).
    top_n : int
        Number of top traders to return.

    Returns a JSON-serialisable list of wallet stat dicts.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession() as session:
        # Step 1: fetch active markets
        markets = await fetch_active_markets(session)

        if not markets:
            logger.warning("No markets returned – returning empty result")
            return []

        # Step 2: collect candidate wallet addresses from trade history
        candidates, wallet_profiles = await collect_candidate_wallets(session, markets, semaphore)

        if not candidates:
            logger.warning("No candidate wallets found")
            return []

        # Step 3: fetch each wallet's full position history concurrently
        logger.info("Fetching position history for %d candidates …", len(candidates))

        async def _positions_for(wallet: str) -> dict[str, Any] | None:
            async with semaphore:
                positions, raw_count = await fetch_wallet_positions(session, wallet, market_cache)
            return _build_wallet_stats(wallet, positions, raw_trade_count=raw_count, profile=wallet_profiles.get(wallet))

        stat_tasks = [_positions_for(w) for w in candidates]
        all_stats = await asyncio.gather(*stat_tasks)

    # Step 4: filter out wallets that didn't pass criteria (returned None)
    qualified = [s for s in all_stats if s is not None]
    logger.info("%d wallets passed all filters", len(qualified))

    # Step 5: rank by composite score (primary sort)
    qualified.sort(key=lambda x: x["composite_score"], reverse=True)
    top_traders = qualified[:top_n]

    logger.info("Top %d traders selected", len(top_traders))
    return top_traders


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    async def _main():
        cache = MarketCache()
        await cache.start()
        traders = await discover_top_traders(market_cache=cache)
        await cache.stop()
        print(json.dumps(traders, indent=2))

    asyncio.run(_main())
