"""
Trade Detection & Execution Service (v3)
========================================
Polls the global Polymarket `/trades` endpoint to detect both BUY and SELL
activity from watched wallets without triggering rate limits.

Key features:
  - 1 request per second to `/trades` instead of polling per wallet
  - In-memory regex/set filtering
  - Redis deduplication to avoid double-processing
  - Detects "SELL" orders to emit EXIT signals for open positions
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
import json
from typing import Any, Callable, Awaitable

import aiohttp

from .market_cache import MarketCache
from database.redis_client import redis_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POLYMARKET_DATA_API = os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com")
POLL_INTERVAL_MS: int = int(os.getenv("POLL_INTERVAL_MS", "1000"))
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "10"))
TRADES_DEDUP_KEY = "polybot:trades:seen"

# ---------------------------------------------------------------------------
# Standardized trade signal
# ---------------------------------------------------------------------------
def make_trade_signal(
    wallet: str,
    condition_id: str,
    outcome: str,
    side: str,
    entry_price: float,
    size_usdc: float,
    title: str = "",
    transaction_hash: str = "",
    signal_type: str = "ENTRY", # "ENTRY" or "EXIT"
) -> dict[str, Any]:
    """Create a standardized trade signal dict."""
    return {
        "signal_id": str(uuid.uuid4()),
        "signal_type": signal_type.upper(),
        "wallet": wallet.lower(),
        "condition_id": condition_id,
        "outcome": outcome,
        "side": side.upper(),
        "entry_price": round(entry_price, 6),
        "size_usdc": round(size_usdc, 4),
        "title": title,
        "transaction_hash": transaction_hash,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Global Trade Detection Service
# ---------------------------------------------------------------------------
class TradeDetectionService:
    """
    Monitors global Polymarket trades and filters for watched wallets.
    """

    def __init__(
        self,
        market_cache: MarketCache,
        signal_callback: Callable[[dict[str, Any]], Awaitable[None]],
        poll_interval_ms: int = POLL_INTERVAL_MS,
    ) -> None:
        self._market_cache = market_cache
        self._signal_callback = signal_callback
        self._poll_interval_ms = poll_interval_ms
        self._watched_wallets: set[str] = set()
        self._task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def set_watched_wallets(self, wallets: list[str]) -> None:
        """Update the set of wallets to monitor (fast lookups)."""
        self._watched_wallets = {w.lower() for w in wallets}
        logger.info("Trade detection now watching %d wallets", len(self._watched_wallets))

    @property
    def watched_count(self) -> int:
        return len(self._watched_wallets)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        
        await redis_db.connect()
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("TradeDetectionService v3 started (global poll_interval=%dms)", self._poll_interval_ms)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("TradeDetectionService v3 stopped")

    # ------------------------------------------------------------------
    # Core monitoring loop
    # ------------------------------------------------------------------
    async def _monitor_loop(self) -> None:
        """Continuously poll global trades."""
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    if self._watched_wallets:
                        await self._poll_global_trades(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error("Trade detection cycle error: %s", exc)

                await asyncio.sleep(self._poll_interval_ms / 1000.0)

    async def _poll_global_trades(self, session: aiohttp.ClientSession) -> None:
        """Fetch latest sequence of all trades across polymarket and filter."""
        try:
            async with session.get(
                f"{POLYMARKET_DATA_API}/trades",
                params={"limit": 50},  # Fetch last 50 trades globally
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 429:
                    logger.warning("Global trades rate-limited, backing off")
                    await asyncio.sleep(2.0)
                    return
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to fetch global trades: %s", exc)
            return

        entries = data if isinstance(data, list) else data.get("data", [])
        
        # Sort oldest to newest
        entries.sort(key=lambda x: x.get("timestamp", ""))

        new_signals = 0
        for e in entries:
            wallet = str(e.get("maker", e.get("proxyWallet", e.get("user", "")))).lower()
            if wallet not in self._watched_wallets:
                continue

            # It's a watched wallet!
            tx_hash = e.get("transactionHash", "")
            trade_id = tx_hash or f"{wallet}:{e.get('timestamp', '')}"
            
            # Redis dedup check
            added = await redis_db.set_add(TRADES_DEDUP_KEY, trade_id)
            if not added:
                continue # Already seen

            side = e.get("side", "BUY").upper()
            
            # If side == SELL, this is an EXIT signal. If side == BUY, it's an ENTRY.
            signal_type = "EXIT" if side == "SELL" else "ENTRY"

            signal = make_trade_signal(
                wallet=wallet,
                condition_id=e.get("conditionId", ""),
                outcome=e.get("outcome", ""),
                side=side,
                entry_price=float(e.get("price", 0.5)),
                size_usdc=float(e.get("usdcSize", e.get("size", 0))),
                title=e.get("title", ""),
                transaction_hash=tx_hash,
                signal_type=signal_type
            )
            
            try:
                await self._signal_callback(signal)
                new_signals += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Signal callback error: %s", exc)

        if new_signals > 0:
            logger.info("Emitted %d new trade signal(s) from global stream", new_signals)
