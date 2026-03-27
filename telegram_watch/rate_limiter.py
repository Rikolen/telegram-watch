"""Rate protection suite for Telegram message sending (7-layer system).

This module implements a composable, self-contained rate-limiting system
designed to keep Telegram user-account message sending well within safe
limits.  Each layer addresses a different failure mode:

    L1 — SlidingWindowCounter
         Hard caps on sends per minute / hour / day using a deque of
         monotonic timestamps.  ``acquire()`` sleeps until the oldest
         entry in a full window expires.

    L2 — JitteredDelay
         Enforces a minimum gap between consecutive sends, adding
         uniform random jitter (±1 s) to avoid predictable cadence.

    L3 — MediaExtraDelay
         Adds extra seconds when the message contains media (photos,
         documents) because Telegram rate-limits media uploads more
         aggressively.

    L4 — Long-period caps
         Per-hour and per-day limits, implemented inside
         SlidingWindowCounter (shared with L1).

    L5 — ExponentialBackoff
         On FloodWait errors the wait time is multiplied by a
         back-off factor that doubles on each consecutive FloodWait
         (capped at 16×) and resets after 5 minutes of silence.

    L6 — CircuitBreaker
         If 3+ FloodWait errors occur within a 10-minute window the
         breaker trips and blocks ALL sends for a cooldown period
         (default 30 min).  Callers receive ``CircuitBrokenError``
         so they can fire a Bark alert.

    L7 — WarmupThrottle
         For the first N minutes after initialisation the per-minute
         cap is overridden with a lower value, giving the session time
         to "warm up" before hitting normal throughput.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CircuitBrokenError(Exception):
    """Raised when the circuit breaker is tripped.

    Attributes
    ----------
    remaining_seconds : float
        Approximate seconds until the breaker auto-resets.
    """

    def __init__(self, remaining_seconds: float) -> None:
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit breaker is open — all sends blocked for "
            f"{remaining_seconds:.0f} more seconds"
        )


# ---------------------------------------------------------------------------
# L1 + L4 — SlidingWindowCounter
# ---------------------------------------------------------------------------

@dataclass
class _Window:
    """A single sliding-window counter."""

    name: str
    max_count: int
    span_seconds: float
    timestamps: deque[float] = field(default_factory=deque)

    def _purge(self, now: float) -> None:
        cutoff = now - self.span_seconds
        while self.timestamps and self.timestamps[0] <= cutoff:
            self.timestamps.popleft()

    def is_full(self, now: float) -> bool:
        self._purge(now)
        return len(self.timestamps) >= self.max_count

    def seconds_until_free(self, now: float) -> float:
        """Return how long to wait before the window has capacity."""
        self._purge(now)
        if len(self.timestamps) < self.max_count:
            return 0.0
        oldest = self.timestamps[0]
        return max(0.0, (oldest + self.span_seconds) - now)

    def record(self, now: float) -> None:
        self.timestamps.append(now)

    @property
    def current_count(self) -> int:
        self._purge(time.monotonic())
        return len(self.timestamps)


class _SlidingWindowCounter:
    """L1 + L4: per-minute, per-hour, and per-day sliding-window caps."""

    def __init__(
        self,
        per_minute: int,
        per_hour: int,
        per_day: int,
    ) -> None:
        self._windows: list[_Window] = [
            _Window("per_minute", per_minute, 60.0),
            _Window("per_hour", per_hour, 3600.0),
            _Window("per_day", per_day, 86400.0),
        ]

    @property
    def per_minute_window(self) -> _Window:
        return self._windows[0]

    async def acquire(self) -> None:
        """Sleep until every window has capacity."""
        while True:
            now = time.monotonic()
            max_wait = 0.0
            for w in self._windows:
                wait = w.seconds_until_free(now)
                if wait > max_wait:
                    max_wait = wait
            if max_wait <= 0.0:
                return
            logger.info(
                "SlidingWindowCounter: sleeping %.1f s (window at capacity)",
                max_wait,
            )
            await asyncio.sleep(max_wait)

    def record(self, now: float) -> None:
        for w in self._windows:
            w.record(now)

    def status(self) -> str:
        parts: list[str] = []
        for w in self._windows:
            parts.append(f"{w.name}={w.current_count}/{w.max_count}")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# L2 — JitteredDelay
# ---------------------------------------------------------------------------

class _JitteredDelay:
    """L2: minimum inter-send gap with random jitter."""

    def __init__(self, min_interval_sec: float) -> None:
        self._min_interval = min_interval_sec
        self._last_send: float | None = None

    async def acquire(self) -> None:
        if self._last_send is None:
            return
        now = time.monotonic()
        jitter = random.uniform(-1.0, 1.0)
        required_gap = max(self._min_interval, self._min_interval + jitter)
        elapsed = now - self._last_send
        if elapsed < required_gap:
            wait = required_gap - elapsed
            logger.debug("JitteredDelay: sleeping %.2f s", wait)
            await asyncio.sleep(wait)

    def record(self, now: float) -> None:
        self._last_send = now


# ---------------------------------------------------------------------------
# L3 — MediaExtraDelay
# ---------------------------------------------------------------------------

class _MediaExtraDelay:
    """L3: additional delay for messages with media attachments."""

    def __init__(self, extra_sec: float) -> None:
        self._extra_sec = extra_sec

    async def acquire(self, has_media: bool) -> None:
        if has_media and self._extra_sec > 0:
            logger.debug("MediaExtraDelay: sleeping %.2f s", self._extra_sec)
            await asyncio.sleep(self._extra_sec)


# ---------------------------------------------------------------------------
# L5 — ExponentialBackoff
# ---------------------------------------------------------------------------

class _ExponentialBackoff:
    """L5: exponential back-off on FloodWait errors."""

    _MAX_MULTIPLIER: int = 16
    _RESET_AFTER_SEC: float = 300.0  # 5 minutes

    def __init__(self) -> None:
        self._multiplier: int = 1
        self._last_flood: float | None = None

    def compute_wait(self, flood_wait_seconds: int) -> float:
        """Return adjusted wait time and bump the multiplier."""
        now = time.monotonic()

        # Reset multiplier if enough quiet time has passed.
        if (
            self._last_flood is not None
            and (now - self._last_flood) >= self._RESET_AFTER_SEC
        ):
            self._multiplier = 1

        wait = flood_wait_seconds * self._multiplier
        self._multiplier = min(self._multiplier * 2, self._MAX_MULTIPLIER)
        self._last_flood = now
        logger.warning(
            "ExponentialBackoff: FloodWait %d s × %d = %.0f s",
            flood_wait_seconds,
            self._multiplier // 2 or 1,
            wait,
        )
        return float(wait)

    def maybe_reset(self) -> None:
        """Reset multiplier if enough quiet time has passed."""
        if self._last_flood is None:
            return
        if (time.monotonic() - self._last_flood) >= self._RESET_AFTER_SEC:
            self._multiplier = 1

    @property
    def multiplier(self) -> int:
        self.maybe_reset()
        return self._multiplier


# ---------------------------------------------------------------------------
# L6 — CircuitBreaker
# ---------------------------------------------------------------------------

class _CircuitBreaker:
    """L6: trip after repeated FloodWait errors."""

    _WINDOW_SEC: float = 600.0  # 10 minutes
    _THRESHOLD: int = 3

    def __init__(self, cooldown_minutes: float = 30.0) -> None:
        self._cooldown_sec: float = cooldown_minutes * 60.0
        self._flood_times: deque[float] = deque()
        self._tripped_at: float | None = None

    def _purge(self, now: float) -> None:
        cutoff = now - self._WINDOW_SEC
        while self._flood_times and self._flood_times[0] < cutoff:
            self._flood_times.popleft()

    def record_flood(self) -> None:
        now = time.monotonic()
        self._flood_times.append(now)
        self._purge(now)
        if len(self._flood_times) >= self._THRESHOLD:
            self._tripped_at = now
            logger.critical(
                "CircuitBreaker TRIPPED: %d FloodWaits in 10 min — "
                "blocking sends for %.0f min",
                len(self._flood_times),
                self._cooldown_sec / 60.0,
            )

    def is_open(self) -> bool:
        """Return True when the breaker is tripped (sends must be blocked)."""
        if self._tripped_at is None:
            return False
        elapsed = time.monotonic() - self._tripped_at
        if elapsed >= self._cooldown_sec:
            # Cooldown expired — auto-reset.
            self._tripped_at = None
            self._flood_times.clear()
            logger.info("CircuitBreaker auto-reset after cooldown")
            return False
        return True

    def remaining_seconds(self) -> float:
        if self._tripped_at is None:
            return 0.0
        return max(0.0, self._cooldown_sec - (time.monotonic() - self._tripped_at))

    @property
    def recent_floods(self) -> int:
        self._purge(time.monotonic())
        return len(self._flood_times)


# ---------------------------------------------------------------------------
# L7 — WarmupThrottle
# ---------------------------------------------------------------------------

class _WarmupThrottle:
    """L7: reduce per-minute cap during an initial warmup period."""

    def __init__(self, warmup_minutes: float, warmup_rate: int) -> None:
        self._warmup_sec = warmup_minutes * 60.0
        self._warmup_rate = warmup_rate
        self._start = time.monotonic()

    def is_active(self) -> bool:
        return (time.monotonic() - self._start) < self._warmup_sec

    @property
    def effective_per_minute(self) -> int:
        return self._warmup_rate

    @property
    def warmup_remaining_sec(self) -> float:
        remaining = self._warmup_sec - (time.monotonic() - self._start)
        return max(0.0, remaining)


# ---------------------------------------------------------------------------
# Public API — RateProtectionSuite
# ---------------------------------------------------------------------------

class RateProtectionSuite:
    """Composable 7-layer rate protection for Telegram message sending.

    Usage::

        rps = RateProtectionSuite()

        # Before every send:
        await rps.acquire(has_media=msg_has_photo)
        try:
            await client.send_message(...)
        except FloodWaitError as exc:
            rps.record_flood_wait(exc.seconds)
            raise
        else:
            rps.record_send()
    """

    def __init__(
        self,
        *,
        rate_limit_per_minute: int = 20,
        rate_limit_per_hour: int = 200,
        rate_limit_per_day: int = 1000,
        min_interval_sec: float = 3.0,
        media_extra_delay_sec: float = 2.0,
        warmup_minutes: float = 5.0,
        warmup_rate: int = 5,
        cooldown_minutes: float = 30.0,
    ) -> None:
        # Store original per-minute cap for warmup override.
        self._base_per_minute = rate_limit_per_minute

        # L1 + L4
        self._sliding = _SlidingWindowCounter(
            per_minute=rate_limit_per_minute,
            per_hour=rate_limit_per_hour,
            per_day=rate_limit_per_day,
        )

        # L2
        self._jitter = _JitteredDelay(min_interval_sec)

        # L3
        self._media = _MediaExtraDelay(media_extra_delay_sec)

        # L5
        self._backoff = _ExponentialBackoff()

        # L6
        self._breaker = _CircuitBreaker(cooldown_minutes=cooldown_minutes)

        # L7
        self._warmup = _WarmupThrottle(warmup_minutes, warmup_rate)

    # -- public interface ---------------------------------------------------

    async def acquire(self, has_media: bool = False) -> None:
        """Wait until it is safe to send.  Call before each send.

        Raises
        ------
        CircuitBrokenError
            If the circuit breaker is currently tripped.
        """
        # L6 — check breaker first (cheapest).
        if self._breaker.is_open():
            raise CircuitBrokenError(self._breaker.remaining_seconds())

        # L5 — reset backoff multiplier if enough quiet time elapsed.
        self._backoff.maybe_reset()

        # L7 — dynamically lower the per-minute window during warmup.
        if self._warmup.is_active():
            self._sliding.per_minute_window.max_count = (
                self._warmup.effective_per_minute
            )
        else:
            self._sliding.per_minute_window.max_count = self._base_per_minute

        # L1 + L4 — sliding windows.
        await self._sliding.acquire()

        # L2 — jittered inter-send delay.
        await self._jitter.acquire()

        # L3 — media extra delay.
        await self._media.acquire(has_media)

    def record_send(self) -> None:
        """Record a successful send.  Call after each ``send_message``."""
        now = time.monotonic()
        self._sliding.record(now)
        self._jitter.record(now)

    def record_flood_wait(self, wait_seconds: int) -> float:
        """Record a FloodWait error from Telegram.

        Returns the backoff-adjusted wait time (>= *wait_seconds*).
        The caller should sleep for at least this long.
        """
        adjusted = self._backoff.compute_wait(wait_seconds)
        self._breaker.record_flood()
        return adjusted

    def is_circuit_broken(self) -> bool:
        """Return ``True`` if the circuit breaker is currently tripped."""
        return self._breaker.is_open()

    def get_status_summary(self) -> str:
        """Return a human-readable summary of current protection state."""
        lines: list[str] = ["Rate Protection Suite status:"]

        # Windows
        lines.append(f"  Sliding windows: {self._sliding.status()}")

        # Warmup
        if self._warmup.is_active():
            lines.append(
                f"  Warmup: ACTIVE (per-min cap "
                f"= {self._warmup.effective_per_minute}, "
                f"{self._warmup.warmup_remaining_sec:.0f} s remaining)"
            )
        else:
            lines.append("  Warmup: complete")

        # Backoff
        lines.append(
            f"  Backoff multiplier: {self._backoff.multiplier}x"
        )

        # Circuit breaker
        if self._breaker.is_open():
            lines.append(
                f"  Circuit breaker: OPEN "
                f"({self._breaker.remaining_seconds():.0f} s remaining)"
            )
        else:
            lines.append(
                f"  Circuit breaker: closed "
                f"(recent floods: {self._breaker.recent_floods})"
            )

        return "\n".join(lines)
