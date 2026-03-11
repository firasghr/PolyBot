"""
Trade Execution Module
======================
Monitors Polymarket trades from selected wallets in real-time (<1 s latency),
checks the Risk Management module for position sizing, then executes the same
trade on Polymarket via the CLOB API.

Key features:
  - Async polling loop targeting <1 s detection latency
  - Slippage & order-book-depth adjustments
  - Structured trade logging (timestamp, wallet, entry price, size, EV)
  - Automatic retry with exponential back-off on failed executions
  - Graceful shutdown on SIGINT / SIGTERM
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from typing import Any

import aiohttp

from .risk_management import calculate_position_sizes

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POLYMARKET_CLOB_API = os.getenv("POLYMARKET_CLOB_API", "https://clob.polymarket.com")
POLYMARKET_CLOB_API_KEY = os.getenv("POLYMARKET_CLOB_API_KEY", "")
POLL_INTERVAL_MS: int = int(os.getenv("POLL_INTERVAL_MS", "500"))  # 500 ms ≈ <1 s
MAX_RETRIES: int = int(os.getenv("MAX_EXECUTION_RETRIES", "3"))
RETRY_BACKOFF: float = float(os.getenv("RETRY_BACKOFF_SECONDS", "1.0"))
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "10"))
MIN_ORDER_BOOK_DEPTH: float = float(os.getenv("MIN_ORDER_BOOK_DEPTH", "100.0"))  # USDC
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

if DRY_RUN:
    logger.warning("*** DRY_RUN MODE ACTIVE – no real trades will be submitted ***")


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_seen_trade_ids: set[str] = set()   # dedup guard
_shutdown_event = asyncio.Event()   # set by signal handler to stop loops


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
def _register_signal_handlers() -> None:
    """Register SIGINT / SIGTERM to trigger a clean shutdown."""
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_event.set)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
) -> Any:
    try:
        async with session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("GET %s failed: %s", url, exc)
        return None


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    headers: dict | None = None,
) -> Any:
    try:
        async with session.post(
            url,
            json=payload,
            headers=headers or {},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("POST %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Order book helpers
# ---------------------------------------------------------------------------
async def fetch_order_book(
    session: aiohttp.ClientSession,
    token_id: str,
) -> dict:
    """Fetch current order book for a CLOB token ID."""
    data = await _get_json(
        session,
        f"{POLYMARKET_CLOB_API}/book",
        params={"token_id": token_id},
    )
    return data or {"bids": [], "asks": []}


def _available_depth(order_book: dict, side: str) -> float:
    """Sum USDC size available on the requested side ('bids' or 'asks')."""
    levels = order_book.get(side, [])
    return sum(float(lvl.get("size", 0)) * float(lvl.get("price", 0)) for lvl in levels)


def _best_price(order_book: dict, side: str) -> float:
    """Return best available price on the given side, or 0.0 if empty."""
    levels = order_book.get(side, [])
    if not levels:
        return 0.0
    # bids are sorted descending, asks ascending (CLOB convention)
    return float(levels[0].get("price", 0))


# ---------------------------------------------------------------------------
# Trade detection
# ---------------------------------------------------------------------------
async def fetch_recent_trades(
    session: aiohttp.ClientSession,
    wallet: str,
    limit: int = 20,
) -> list[dict]:
    """Fetch the most recent trades for a watched wallet."""
    data = await _get_json(
        session,
        f"{POLYMARKET_CLOB_API}/trades",
        params={"maker": wallet, "limit": limit},
    )
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def detect_new_trades(raw_trades: list[dict]) -> list[dict]:
    """
    Filter out already-seen trades using a global dedup set.

    Returns only genuinely new trades and updates the seen set.
    """
    new: list[dict] = []
    for trade in raw_trades:
        tid = str(trade.get("id", trade.get("transactionHash", "")))
        if tid and tid not in _seen_trade_ids:
            _seen_trade_ids.add(tid)
            new.append(trade)
    return new


# ---------------------------------------------------------------------------
# Expected Value computation
# ---------------------------------------------------------------------------
def compute_expected_value(
    entry_price: float,
    size_usdc: float,
    win_rate: float,
) -> float:
    """
    Approximate EV for a Polymarket YES/NO binary outcome.

    EV = win_rate * (1 - entry_price) * size_usdc
       - (1 - win_rate) * entry_price * size_usdc

    (Assumes the payout is $1 per share, entry_price is in [0, 1].)
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    payout = (1.0 - entry_price)
    ev = win_rate * payout * size_usdc - (1.0 - win_rate) * entry_price * size_usdc
    return round(ev, 4)


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------
async def submit_order(
    session: aiohttp.ClientSession,
    token_id: str,
    side: str,  # "BUY" | "SELL"
    size_usdc: float,
    price: float,
) -> dict[str, Any]:
    """
    Submit a limit order to the Polymarket CLOB.

    In DRY_RUN mode the order is not actually sent to the exchange.
    """
    order_id = str(uuid.uuid4())
    payload = {
        "token_id": token_id,
        "side": side,
        "size": round(size_usdc, 2),
        "price": round(price, 6),
        "type": "LIMIT",
        "client_order_id": order_id,
    }

    if DRY_RUN:
        logger.info("[DRY RUN] Would submit order: %s", payload)
        return {"status": "dry_run", "order_id": order_id, "payload": payload}

    headers = {"Authorization": f"Bearer {POLYMARKET_CLOB_API_KEY}"}
    result = await _post_json(
        session, f"{POLYMARKET_CLOB_API}/order", payload, headers
    )
    return result or {"status": "error", "order_id": order_id}


async def execute_with_retry(
    session: aiohttp.ClientSession,
    token_id: str,
    side: str,
    size_usdc: float,
    price: float,
    retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Execute an order with automatic exponential-backoff retries."""
    for attempt in range(1, retries + 1):
        result = await submit_order(session, token_id, side, size_usdc, price)
        if result and result.get("status") not in ("error", None):
            logger.info("Order executed on attempt %d: %s", attempt, result.get("order_id"))
            return result
        if attempt < retries:
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            logger.warning("Execution attempt %d/%d failed. Retrying in %.1fs …", attempt, retries, wait)
            await asyncio.sleep(wait)

    logger.error("All %d execution attempts failed for token %s", retries, token_id)
    return {"status": "failed", "token_id": token_id}


# ---------------------------------------------------------------------------
# Trade processing pipeline
# ---------------------------------------------------------------------------
async def process_trade(
    session: aiohttp.ClientSession,
    trade: dict,
    sizing_map: dict[str, float],
    wallet_stats: list[dict],
) -> dict[str, Any] | None:
    """
    Process a single detected trade:
      1. Fetch order book for depth / slippage check
      2. Determine position size from sizing_map
      3. Compute EV
      4. Execute (or skip on insufficient depth)
      5. Return structured trade log entry
    """
    token_id = str(trade.get("asset_id", trade.get("tokenId", "")))
    side = str(trade.get("side", "BUY")).upper()
    wallet = str(trade.get("maker", trade.get("user", ""))).lower()
    market = str(trade.get("market", ""))

    if not token_id:
        logger.warning("Trade missing token_id – skipping: %s", trade)
        return None

    # Fetch order book concurrently with stats lookup
    order_book = await fetch_order_book(session, token_id)
    depth_side = "asks" if side == "BUY" else "bids"
    available_depth = _available_depth(order_book, depth_side)
    best_px = _best_price(order_book, depth_side)

    if available_depth < MIN_ORDER_BOOK_DEPTH:
        logger.warning(
            "Insufficient order book depth (%.2f USDC) for token %s – skipping",
            available_depth, token_id,
        )
        return None

    # Look up recommended size from risk module output
    size_usdc = sizing_map.get(wallet, 0.0)
    if size_usdc <= 0:
        logger.info("No sizing found for wallet %s – skipping", wallet[:10])
        return None

    # Guard: don't exceed available depth
    size_usdc = min(size_usdc, available_depth * 0.8)

    # Resolve win_rate for EV calculation
    win_rate = next(
        (s["win_rate"] for s in wallet_stats if s["wallet"].lower() == wallet),
        0.6,
    )
    ev = compute_expected_value(best_px, size_usdc, win_rate)

    timestamp = time.time()
    result = await execute_with_retry(session, token_id, side, size_usdc, best_px)

    log_entry: dict[str, Any] = {
        "timestamp": timestamp,
        "trade_id": result.get("order_id", ""),
        "wallet": wallet,
        "market": market,
        "token_id": token_id,
        "side": side,
        "entry_price": round(best_px, 6),
        "position_size_usdc": round(size_usdc, 4),
        "expected_ev_usdc": ev,
        "execution_status": result.get("status", "unknown"),
    }
    logger.info(
        "Trade executed: wallet=%s  market=%s  side=%s  size=$%.2f  price=%.4f  EV=$%.2f  status=%s",
        wallet[:10] + "…",
        market[:20],
        side,
        size_usdc,
        best_px,
        ev,
        log_entry["execution_status"],
    )
    return log_entry


# ---------------------------------------------------------------------------
# Main monitoring loop
# ---------------------------------------------------------------------------
async def monitor_wallets(
    wallet_stats: list[dict],
    portfolio_value_usdc: float,
    kelly_mode: str = "half",
    poll_interval_ms: int = POLL_INTERVAL_MS,
) -> None:
    """
    Continuously poll watched wallets for new trades, check sizing, and
    execute copy trades.

    Runs until *_shutdown_event* is set (SIGINT / SIGTERM).
    """
    _register_signal_handlers()

    # Pre-compute position sizes from risk module
    sizing_output = calculate_position_sizes(wallet_stats, portfolio_value_usdc, kelly_mode)
    sizing_map: dict[str, float] = {
        s["wallet"].lower(): s["effective_size_usdc"] for s in sizing_output
    }
    watched_wallets = [s["wallet"].lower() for s in wallet_stats]

    logger.info(
        "Starting monitor loop for %d wallets (poll_interval=%dms)",
        len(watched_wallets), poll_interval_ms,
    )

    trade_log: list[dict] = []

    async with aiohttp.ClientSession() as session:
        while not _shutdown_event.is_set():
            cycle_start = time.monotonic()

            # Poll all wallets concurrently
            fetch_tasks = [
                asyncio.create_task(fetch_recent_trades(session, w))
                for w in watched_wallets
            ]
            all_raw = await asyncio.gather(*fetch_tasks)

            new_trades: list[dict] = []
            for raw_list in all_raw:
                new_trades.extend(detect_new_trades(raw_list))

            if new_trades:
                logger.info("Detected %d new trade(s) this cycle", len(new_trades))

            process_tasks = [
                asyncio.create_task(
                    process_trade(session, t, sizing_map, wallet_stats)
                )
                for t in new_trades
            ]
            results = await asyncio.gather(*process_tasks)
            trade_log.extend(e for e in results if e is not None)

            # Sleep for remainder of polling interval
            elapsed_ms = (time.monotonic() - cycle_start) * 1000
            sleep_ms = max(0.0, poll_interval_ms - elapsed_ms)
            await asyncio.sleep(sleep_ms / 1000.0)

    logger.info("Monitor loop stopped. Total trades logged: %d", len(trade_log))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    sample_stats = [
        {
            "wallet": "0xABC123",
            "win_rate": 0.72,
            "avg_position_size_usdc": 1500.0,
            "sharpe_ratio": 1.8,
            "market_focus": "crypto",
            "trade_count": 210,
        }
    ]
    asyncio.run(monitor_wallets(sample_stats, portfolio_value_usdc=50_000.0))
