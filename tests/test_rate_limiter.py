"""Tests for telegram_watch.rate_limiter and RealtimeConfig parsing."""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from telegram_watch.config import ConfigError, load_config
from telegram_watch.rate_limiter import (
    CircuitBrokenError,
    RateProtectionSuite,
    _CircuitBreaker,
    _ExponentialBackoff,
    _JitteredDelay,
    _MediaExtraDelay,
    _SlidingWindowCounter,
    _WarmupThrottle,
    _Window,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str, *, include_version: bool = True) -> Path:
    """Write a TOML config file for testing."""
    cfg_path = tmp_path / "config.toml"
    content = dedent(body).lstrip()
    if include_version and "config_version" not in content:
        content = f"config_version = 1.0\n\n{content}"
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


def _base_toml(*, realtime_section: str = "") -> str:
    """Return a minimal valid TOML config with an optional [realtime] section."""
    return f"""\
    [telegram]
    api_id = 42
    api_hash = "abcdefghijk"

    [target]
    target_chat_id = -1001
    tracked_user_ids = [123]

    [control]
    control_chat_id = -1002

    [storage]
    db_path = "data/app.sqlite3"
    media_dir = "data/media"

    {realtime_section}
    """


# ===================================================================
# L1 + L4 — SlidingWindowCounter
# ===================================================================


class TestSlidingWindow:
    """L1: sliding-window rate limiting."""

    def test_sends_within_limit_pass(self):
        """Sends below the cap should not block."""
        sw = _SlidingWindowCounter(per_minute=5, per_hour=100, per_day=1000)
        now = 1000.0
        for i in range(5):
            # All five should report 0 wait
            wait = sw.per_minute_window.seconds_until_free(now)
            assert wait == 0.0
            sw.record(now + i * 0.01)

    def test_window_full_reports_positive_wait(self):
        """When the window is at capacity, seconds_until_free > 0."""
        sw = _SlidingWindowCounter(per_minute=3, per_hour=100, per_day=1000)
        base = 1000.0
        # Fill the per-minute window
        for i in range(3):
            sw.record(base + i)
        # Now querying at base+2 should show window is full
        wait = sw.per_minute_window.seconds_until_free(base + 2)
        assert wait > 0.0
        # The wait should be until the oldest entry (base) expires out of the
        # 60-second window, i.e. approximately (base + 60) - (base + 2) = 58 s
        assert 57.0 < wait < 61.0

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_full(self):
        """acquire() should sleep when the window is at capacity."""
        sw = _SlidingWindowCounter(per_minute=2, per_hour=100, per_day=1000)
        base = 1000.0
        sw.record(base)
        sw.record(base + 0.1)

        # Patch time.monotonic to simulate being right after the second send,
        # then jump forward past the window expiry so the loop exits.
        call_count = 0

        def advancing_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                return base + 0.2  # still within window
            return base + 61.0  # past window expiry

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        with patch("time.monotonic", side_effect=advancing_monotonic):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await sw.acquire()

        # acquire() should have slept at least once while the window was full
        assert len(sleep_calls) >= 1
        assert sleep_calls[0] > 0.0

    def test_status_string(self):
        sw = _SlidingWindowCounter(per_minute=20, per_hour=200, per_day=1000)
        status = sw.status()
        assert "per_minute=" in status
        assert "per_hour=" in status
        assert "per_day=" in status


# ===================================================================
# L2 — JitteredDelay
# ===================================================================


class TestJitteredDelay:
    """L2: minimum inter-send gap with jitter."""

    @pytest.mark.asyncio
    async def test_first_send_no_delay(self):
        """The very first send should not wait at all."""
        jd = _JitteredDelay(min_interval_sec=5.0)
        sleep_called = False

        async def mock_sleep(seconds: float) -> None:
            nonlocal sleep_called
            sleep_called = True

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await jd.acquire()
        assert not sleep_called

    @pytest.mark.asyncio
    async def test_second_send_waits(self):
        """After recording a send, the next acquire should sleep."""
        jd = _JitteredDelay(min_interval_sec=3.0)
        base = 1000.0
        jd.record(base)

        slept_for: float | None = None

        async def mock_sleep(seconds: float) -> None:
            nonlocal slept_for
            slept_for = seconds

        # Simulate calling acquire 0.5 s after the last send.
        # With min_interval=3 and jitter in [-1, 1], the required gap is
        # between 2 and 4.  0.5 elapsed, so wait = gap - 0.5.
        # We fix jitter to 0 so required gap is exactly 3.
        with (
            patch("time.monotonic", return_value=base + 0.5),
            patch("random.uniform", return_value=0.0),
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            await jd.acquire()

        assert slept_for is not None
        assert slept_for == pytest.approx(2.5, abs=0.01)


# ===================================================================
# L3 — MediaExtraDelay
# ===================================================================


class TestMediaExtraDelay:
    """L3: extra delay for media messages."""

    @pytest.mark.asyncio
    async def test_media_true_adds_delay(self):
        """has_media=True should trigger an asyncio.sleep."""
        med = _MediaExtraDelay(extra_sec=2.0)
        slept_for: float | None = None

        async def mock_sleep(seconds: float) -> None:
            nonlocal slept_for
            slept_for = seconds

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await med.acquire(has_media=True)
        assert slept_for == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_media_false_no_delay(self):
        """has_media=False should not sleep."""
        med = _MediaExtraDelay(extra_sec=2.0)
        sleep_called = False

        async def mock_sleep(seconds: float) -> None:
            nonlocal sleep_called
            sleep_called = True

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await med.acquire(has_media=False)
        assert not sleep_called


# ===================================================================
# L5 — ExponentialBackoff
# ===================================================================


class TestExponentialBackoff:
    """L5: exponential back-off on FloodWait errors."""

    def test_first_flood_uses_1x(self):
        """First FloodWait uses 1x multiplier."""
        eb = _ExponentialBackoff()
        with patch("time.monotonic", return_value=1000.0):
            wait = eb.compute_wait(10)
        assert wait == 10.0

    def test_second_flood_uses_2x(self):
        """Second consecutive FloodWait uses 2x multiplier."""
        eb = _ExponentialBackoff()
        with patch("time.monotonic", return_value=1000.0):
            eb.compute_wait(10)
        with patch("time.monotonic", return_value=1010.0):
            wait = eb.compute_wait(10)
        assert wait == 20.0

    def test_multiplier_caps_at_16x(self):
        """Multiplier should not exceed 16x."""
        eb = _ExponentialBackoff()
        t = 1000.0
        for _ in range(10):
            with patch("time.monotonic", return_value=t):
                eb.compute_wait(10)
            t += 1.0
        # After many consecutive floods, multiplier caps at 16
        assert eb._multiplier == 16

    def test_resets_after_5_minutes(self):
        """Multiplier should reset to 1 after 5 minutes of quiet time."""
        eb = _ExponentialBackoff()
        with patch("time.monotonic", return_value=1000.0):
            eb.compute_wait(10)
        with patch("time.monotonic", return_value=1010.0):
            eb.compute_wait(10)
        # Now multiplier is 4; simulate 5+ minutes passing
        with patch("time.monotonic", return_value=1010.0 + 301.0):
            wait = eb.compute_wait(10)
        # Should have reset to 1 first, then multiplied
        assert wait == 10.0

    def test_maybe_reset_with_quiet_time(self):
        """maybe_reset() should reset the multiplier after enough quiet time."""
        eb = _ExponentialBackoff()
        with patch("time.monotonic", return_value=1000.0):
            eb.compute_wait(10)
        assert eb._multiplier == 2
        with patch("time.monotonic", return_value=1301.0):
            eb.maybe_reset()
        assert eb._multiplier == 1

    def test_multiplier_property_respects_reset(self):
        eb = _ExponentialBackoff()
        with patch("time.monotonic", return_value=1000.0):
            eb.compute_wait(10)
        with patch("time.monotonic", return_value=1301.0):
            assert eb.multiplier == 1


# ===================================================================
# L6 — CircuitBreaker
# ===================================================================


class TestCircuitBreaker:
    """L6: trip after repeated FloodWait errors."""

    def test_not_tripped_after_1_flood(self):
        cb = _CircuitBreaker(cooldown_minutes=1.0)
        with patch("time.monotonic", return_value=1000.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1000.5):
            assert not cb.is_open()

    def test_not_tripped_after_2_floods(self):
        cb = _CircuitBreaker(cooldown_minutes=1.0)
        with patch("time.monotonic", return_value=1000.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1001.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1001.5):
            assert not cb.is_open()

    def test_tripped_after_3_floods(self):
        """3 FloodWaits in 10 minutes should trip the breaker."""
        cb = _CircuitBreaker(cooldown_minutes=1.0)
        with patch("time.monotonic", return_value=1000.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1001.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1002.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1002.5):
            assert cb.is_open()

    def test_remaining_seconds_positive_when_tripped(self):
        cb = _CircuitBreaker(cooldown_minutes=1.0)
        for t in (1000.0, 1001.0, 1002.0):
            with patch("time.monotonic", return_value=t):
                cb.record_flood()
        with patch("time.monotonic", return_value=1010.0):
            remaining = cb.remaining_seconds()
        assert remaining > 0.0
        # cooldown is 60 s, elapsed since trip is 8 s, so ~52 s remaining
        assert 50.0 < remaining < 60.0

    def test_auto_reset_after_cooldown(self):
        """Breaker should auto-reset after the cooldown expires."""
        cb = _CircuitBreaker(cooldown_minutes=1.0)  # 60 s cooldown
        for t in (1000.0, 1001.0, 1002.0):
            with patch("time.monotonic", return_value=t):
                cb.record_flood()
        with patch("time.monotonic", return_value=1002.5):
            assert cb.is_open()
        # After cooldown (tripped at 1002, cooldown 60 s, so free at 1062)
        with patch("time.monotonic", return_value=1063.0):
            assert not cb.is_open()

    def test_floods_outside_window_dont_trip(self):
        """Floods >10 min apart should not accumulate."""
        cb = _CircuitBreaker(cooldown_minutes=1.0)
        # First flood at t=0
        with patch("time.monotonic", return_value=0.0):
            cb.record_flood()
        # Second flood at t=500 (first expired from 10-min window)
        with patch("time.monotonic", return_value=700.0):
            cb.record_flood()
        # Third flood at t=1400 (second expired)
        with patch("time.monotonic", return_value=1400.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1400.5):
            assert not cb.is_open()

    def test_recent_floods_property(self):
        cb = _CircuitBreaker()
        with patch("time.monotonic", return_value=1000.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1001.0):
            cb.record_flood()
        with patch("time.monotonic", return_value=1001.5):
            assert cb.recent_floods == 2


# ===================================================================
# L7 — WarmupThrottle
# ===================================================================


class TestWarmupThrottle:
    """L7: reduce per-minute cap during warmup."""

    def test_active_during_warmup(self):
        with patch("time.monotonic", return_value=1000.0):
            wt = _WarmupThrottle(warmup_minutes=5.0, warmup_rate=3)
        with patch("time.monotonic", return_value=1100.0):  # 100 s < 300 s
            assert wt.is_active()
            assert wt.effective_per_minute == 3

    def test_inactive_after_warmup(self):
        with patch("time.monotonic", return_value=1000.0):
            wt = _WarmupThrottle(warmup_minutes=5.0, warmup_rate=3)
        with patch("time.monotonic", return_value=1301.0):  # 301 s > 300 s
            assert not wt.is_active()

    def test_warmup_remaining_sec(self):
        with patch("time.monotonic", return_value=1000.0):
            wt = _WarmupThrottle(warmup_minutes=5.0, warmup_rate=3)
        with patch("time.monotonic", return_value=1100.0):
            remaining = wt.warmup_remaining_sec
        assert remaining == pytest.approx(200.0, abs=1.0)


# ===================================================================
# RateProtectionSuite — integrated
# ===================================================================


class TestRateProtectionSuite:
    """Integration tests for the full 7-layer suite."""

    @pytest.mark.asyncio
    async def test_acquire_raises_when_circuit_broken(self):
        """acquire() should raise CircuitBrokenError when breaker is tripped."""
        rps = RateProtectionSuite(
            rate_limit_per_minute=20,
            cooldown_minutes=1.0,
        )
        # Trip the breaker by recording 3 floods
        for t in (1000.0, 1001.0, 1002.0):
            with patch("time.monotonic", return_value=t):
                rps.record_flood_wait(10)

        with patch("time.monotonic", return_value=1003.0):
            with pytest.raises(CircuitBrokenError) as exc_info:
                await rps.acquire()
            assert exc_info.value.remaining_seconds > 0

    @pytest.mark.asyncio
    async def test_warmup_reduces_per_minute_cap(self):
        """During warmup, per-minute cap should equal warmup_rate."""
        with patch("time.monotonic", return_value=1000.0):
            rps = RateProtectionSuite(
                rate_limit_per_minute=20,
                warmup_minutes=5.0,
                warmup_rate=2,
                min_interval_sec=0.0,
                media_extra_delay_sec=0.0,
            )

        # During warmup, the per_minute_window.max_count should be set to 2
        with (
            patch("time.monotonic", return_value=1010.0),
            patch("asyncio.sleep", new_callable=lambda: _async_noop),
        ):
            await rps.acquire()
        assert rps._sliding.per_minute_window.max_count == 2

    @pytest.mark.asyncio
    async def test_after_warmup_restores_normal_cap(self):
        """After warmup, per-minute cap should revert to base rate."""
        with patch("time.monotonic", return_value=1000.0):
            rps = RateProtectionSuite(
                rate_limit_per_minute=20,
                warmup_minutes=1.0,  # 60 s warmup
                warmup_rate=2,
                min_interval_sec=0.0,
                media_extra_delay_sec=0.0,
            )

        with (
            patch("time.monotonic", return_value=1061.0),  # past warmup
            patch("asyncio.sleep", new_callable=lambda: _async_noop),
        ):
            await rps.acquire()
        assert rps._sliding.per_minute_window.max_count == 20

    def test_record_send(self):
        """record_send() should register timestamps in sliding windows."""
        rps = RateProtectionSuite()
        with patch("time.monotonic", return_value=1000.0):
            rps.record_send()
        assert rps._sliding.per_minute_window.timestamps[-1] == 1000.0

    def test_record_flood_wait_bumps_backoff_and_breaker(self):
        rps = RateProtectionSuite()
        with patch("time.monotonic", return_value=1000.0):
            rps.record_flood_wait(10)
            assert rps._backoff._multiplier == 2
            assert rps._breaker.recent_floods >= 1

    def test_is_circuit_broken(self):
        rps = RateProtectionSuite(cooldown_minutes=1.0)
        with patch("time.monotonic", return_value=1000.0):
            assert not rps.is_circuit_broken()
        for t in (1000.0, 1001.0, 1002.0):
            with patch("time.monotonic", return_value=t):
                rps.record_flood_wait(10)
        with patch("time.monotonic", return_value=1003.0):
            assert rps.is_circuit_broken()


# ===================================================================
# get_status_summary()
# ===================================================================


class TestStatusSummary:
    """get_status_summary() should return informative text."""

    def test_returns_non_empty_string(self):
        rps = RateProtectionSuite()
        summary = rps.get_status_summary()
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_contains_window_counts(self):
        rps = RateProtectionSuite()
        summary = rps.get_status_summary()
        assert "per_minute=" in summary
        assert "per_hour=" in summary

    def test_contains_warmup_status(self):
        with patch("time.monotonic", return_value=1000.0):
            rps = RateProtectionSuite(warmup_minutes=5.0)
        with patch("time.monotonic", return_value=1010.0):
            summary = rps.get_status_summary()
        assert "Warmup" in summary
        assert "ACTIVE" in summary

    def test_contains_warmup_complete(self):
        with patch("time.monotonic", return_value=1000.0):
            rps = RateProtectionSuite(warmup_minutes=0.01)
        with patch("time.monotonic", return_value=2000.0):
            summary = rps.get_status_summary()
        assert "Warmup" in summary
        assert "complete" in summary

    def test_contains_breaker_status_closed(self):
        rps = RateProtectionSuite()
        summary = rps.get_status_summary()
        assert "Circuit breaker" in summary
        assert "closed" in summary

    def test_contains_breaker_status_open(self):
        rps = RateProtectionSuite(cooldown_minutes=1.0)
        for t in (1000.0, 1001.0, 1002.0):
            with patch("time.monotonic", return_value=t):
                rps.record_flood_wait(10)
        with patch("time.monotonic", return_value=1003.0):
            summary = rps.get_status_summary()
        assert "OPEN" in summary

    def test_contains_backoff_multiplier(self):
        rps = RateProtectionSuite()
        summary = rps.get_status_summary()
        assert "Backoff multiplier" in summary


# ===================================================================
# CircuitBrokenError
# ===================================================================


class TestCircuitBrokenError:

    def test_attributes(self):
        err = CircuitBrokenError(120.0)
        assert err.remaining_seconds == 120.0
        assert "120" in str(err)


# ===================================================================
# RealtimeConfig parsing (via load_config)
# ===================================================================


class TestRealtimeConfigParsing:
    """Parsing the [realtime] section of config.toml."""

    def test_defaults_when_section_missing(self, tmp_path: Path):
        """When [realtime] is absent, sensible defaults are applied."""
        cfg_path = _write_config(tmp_path, _base_toml())
        config = load_config(cfg_path)
        rt = config.realtime
        assert rt.push_mode == "interval"
        assert rt.rate_limit_per_minute == 20
        assert rt.rate_limit_per_hour == 200
        assert rt.rate_limit_per_day == 1000
        assert rt.min_interval_sec == 3.0
        assert rt.media_extra_delay_sec == 2.0
        assert rt.warmup_minutes == 5.0
        assert rt.warmup_rate == 5
        assert rt.report_interval_minutes == 120

    def test_custom_values_parsed(self, tmp_path: Path):
        """Custom [realtime] values should be stored correctly."""
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
push_mode = "realtime"
report_interval_minutes = 60
rate_limit_per_minute = 15
rate_limit_per_hour = 150
rate_limit_per_day = 800
min_interval_sec = 5.0
media_extra_delay_sec = 4.0
warmup_minutes = 10.0
warmup_rate = 3
"""
            ),
        )
        config = load_config(cfg_path)
        rt = config.realtime
        assert rt.push_mode == "realtime"
        assert rt.report_interval_minutes == 60
        assert rt.rate_limit_per_minute == 15
        assert rt.rate_limit_per_hour == 150
        assert rt.rate_limit_per_day == 800
        assert rt.min_interval_sec == 5.0
        assert rt.media_extra_delay_sec == 4.0
        assert rt.warmup_minutes == 10.0
        assert rt.warmup_rate == 3

    def test_invalid_push_mode_raises(self, tmp_path: Path):
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
push_mode = "immediate"
"""
            ),
        )
        with pytest.raises(ConfigError, match="push_mode"):
            load_config(cfg_path)

    def test_rate_limit_per_minute_above_30_raises(self, tmp_path: Path):
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
rate_limit_per_minute = 31
"""
            ),
        )
        with pytest.raises(ConfigError, match="rate_limit_per_minute"):
            load_config(cfg_path)

    def test_rate_limit_per_minute_above_25_warns(self, tmp_path: Path):
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
rate_limit_per_minute = 26
"""
            ),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            config = load_config(cfg_path)
        assert config.realtime.rate_limit_per_minute == 26
        flood_warnings = [
            w for w in caught if "aggressive" in str(w.message).lower()
            or "flood" in str(w.message).lower()
        ]
        assert len(flood_warnings) >= 1

    def test_rate_limit_per_minute_below_1_raises(self, tmp_path: Path):
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
rate_limit_per_minute = 0
"""
            ),
        )
        with pytest.raises(ConfigError, match="rate_limit_per_minute"):
            load_config(cfg_path)

    def test_rate_limit_per_minute_25_no_warning(self, tmp_path: Path):
        """Values at 25 or below should not trigger a warning."""
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
rate_limit_per_minute = 25
"""
            ),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_config(cfg_path)
        flood_warnings = [
            w for w in caught if "aggressive" in str(w.message).lower()
            or "flood" in str(w.message).lower()
        ]
        assert len(flood_warnings) == 0

    def test_negative_min_interval_raises(self, tmp_path: Path):
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
min_interval_sec = -1.0
"""
            ),
        )
        with pytest.raises(ConfigError, match="min_interval_sec"):
            load_config(cfg_path)

    def test_negative_warmup_minutes_raises(self, tmp_path: Path):
        cfg_path = _write_config(
            tmp_path,
            _base_toml(
                realtime_section="""\
[realtime]
warmup_minutes = 0
"""
            ),
        )
        with pytest.raises(ConfigError, match="warmup_minutes"):
            load_config(cfg_path)


# ===================================================================
# _Window unit tests
# ===================================================================


class TestWindow:
    """Direct tests on the _Window dataclass."""

    def test_purge_removes_old_entries(self):
        w = _Window(name="test", max_count=10, span_seconds=60.0)
        w.record(100.0)
        w.record(150.0)
        w.record(170.0)
        # At t=170, cutoff is 110 -> entry at 100 should be removed
        assert not w.is_full(170.0)
        w._purge(170.0)
        assert len(w.timestamps) == 2

    def test_is_full(self):
        w = _Window(name="test", max_count=2, span_seconds=60.0)
        w.record(100.0)
        w.record(101.0)
        assert w.is_full(102.0)
        # After window span passes, should no longer be full
        assert not w.is_full(161.0)

    def test_seconds_until_free_zero_when_not_full(self):
        w = _Window(name="test", max_count=5, span_seconds=60.0)
        w.record(100.0)
        assert w.seconds_until_free(100.5) == 0.0

    def test_seconds_until_free_positive_when_full(self):
        w = _Window(name="test", max_count=2, span_seconds=10.0)
        w.record(100.0)
        w.record(101.0)
        wait = w.seconds_until_free(105.0)
        # Oldest (100) + span (10) - now (105) = 5
        assert wait == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Async test helper
# ---------------------------------------------------------------------------


class _async_noop:
    """A callable that returns a no-op coroutine, usable as new_callable for
    patching asyncio.sleep."""

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._noop(*args, **kwargs)

    @staticmethod
    async def _noop(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass
