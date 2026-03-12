"""
Basket Strategy Module
======================
Detects when multiple tracked wallets enter the same market outcome within
a time window, increasing signal confidence and recommended position size.

Design:
  - Records every incoming trade signal with (wallet, condition_id, outcome, timestamp)
  - When checking confluence, counts how many distinct wallets have signaled
    the same (condition_id, outcome) within the last N seconds
  - Returns a confidence multiplier:
      1 wallet  → 1.0× (no change)
      2 wallets → 1.5× size multiplier
      3+ wallets → 2.0× size multiplier

Usage::

    basket = BasketStrategy(time_window_s=300)
    basket.record_signal(wallet="0x...", condition_id="0x...", outcome="Yes", timestamp=time.time())
    result = basket.check_confluence(condition_id="0x...", outcome="Yes")
    adjusted_size = base_size * result.multiplier
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ConfluenceResult:
    """Outcome of a confluence check."""

    matching_wallets: list[str]
    signal_count: int
    multiplier: float


@dataclass
class _SignalRecord:
    """Internal record of a single trade signal."""

    wallet: str
    condition_id: str
    outcome: str
    timestamp: float


class BasketStrategy:
    """
    Multi-wallet confluence detection for copy-trading signals.

    Parameters
    ----------
    time_window_s : int
        Seconds within which signals must occur to count as confluent.
    max_multiplier : float
        Maximum position size multiplier.
    """

    def __init__(
        self,
        time_window_s: int = 300,
        max_multiplier: float = 2.0,
    ) -> None:
        self._time_window = time_window_s
        self._max_multiplier = max_multiplier
        self._signals: list[_SignalRecord] = []

    def record_signal(
        self,
        wallet: str,
        condition_id: str,
        outcome: str,
        timestamp: float | None = None,
    ) -> None:
        """Record an incoming trade signal."""
        ts = timestamp or time.time()
        self._signals.append(_SignalRecord(
            wallet=wallet.lower(),
            condition_id=condition_id,
            outcome=outcome,
            timestamp=ts,
        ))
        self._prune(ts)

    def check_confluence(
        self,
        condition_id: str,
        outcome: str,
    ) -> ConfluenceResult:
        """
        Check how many distinct wallets have signaled the same
        (condition_id, outcome) within the time window.

        Returns a ConfluenceResult with the matching wallets and
        a position size multiplier.
        """
        now = time.time()
        cutoff = now - self._time_window

        matching_wallets: set[str] = set()
        signal_count = 0

        for s in self._signals:
            if (
                s.condition_id == condition_id
                and s.outcome.lower() == outcome.lower()
                and s.timestamp >= cutoff
            ):
                matching_wallets.add(s.wallet)
                signal_count += 1

        n = len(matching_wallets)
        if n >= 3:
            multiplier = self._max_multiplier
        elif n == 2:
            multiplier = 1.5
        else:
            multiplier = 1.0

        if multiplier > 1.0:
            logger.info(
                "Confluence detected: %d wallets on %s outcome=%s → %.1f× multiplier",
                n, condition_id[:10], outcome, multiplier,
            )

        return ConfluenceResult(
            matching_wallets=sorted(matching_wallets),
            signal_count=signal_count,
            multiplier=multiplier,
        )

    def _prune(self, now: float | None = None) -> None:
        """Remove signals older than the time window."""
        cutoff = (now or time.time()) - self._time_window * 2
        self._signals = [s for s in self._signals if s.timestamp >= cutoff]
