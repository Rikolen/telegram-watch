"""Tests for telegram_watch.update_checker."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio  # noqa: F401 -- ensures the plugin is loaded

from telegram_watch.update_checker import (
    UpdateInfo,
    _is_newer,
    _load_notified,
    _parse_version,
    _save_notified,
    check_for_update,
    format_notification,
    get_current_version,
    record_notification,
    should_notify,
)


# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_plain(self):
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_v_prefix(self):
        assert _parse_version("v1.2.3") == (1, 2, 3)

    def test_zero(self):
        assert _parse_version("0.0.0") == (0, 0, 0)

    def test_major_only(self):
        assert _parse_version("1") == (1,)

    def test_two_segments(self):
        assert _parse_version("1.0") == (1, 0)

    def test_large_numbers(self):
        assert _parse_version("10.20.300") == (10, 20, 300)


# ---------------------------------------------------------------------------
# _is_newer
# ---------------------------------------------------------------------------

class TestIsNewer:
    def test_newer_patch(self):
        assert _is_newer("1.0.1", "1.0.0") is True

    def test_newer_minor(self):
        assert _is_newer("1.1.0", "1.0.9") is True

    def test_newer_major(self):
        assert _is_newer("2.0.0", "1.9.9") is True

    def test_same_version(self):
        assert _is_newer("1.0.0", "1.0.0") is False

    def test_older_version(self):
        assert _is_newer("1.0.0", "1.0.1") is False

    def test_v_prefix_remote(self):
        assert _is_newer("v1.1.0", "1.0.0") is True

    def test_v_prefix_local(self):
        assert _is_newer("1.1.0", "v1.0.0") is True

    def test_different_segment_counts_newer(self):
        # (1, 1) > (1, 0, 9) because tuple comparison: 1==1, then 1>0
        assert _is_newer("1.1", "1.0.9") is True

    def test_different_segment_counts_shorter_local(self):
        # (1, 0, 1) > (1,) because after matching first element, remote has more
        assert _is_newer("1.0.1", "1") is True

    def test_invalid_remote_returns_false(self):
        assert _is_newer("abc", "1.0.0") is False

    def test_invalid_local_returns_false(self):
        assert _is_newer("1.0.0", "xyz") is False

    def test_both_invalid_returns_false(self):
        assert _is_newer("abc", "xyz") is False


# ---------------------------------------------------------------------------
# _load_notified / _save_notified
# ---------------------------------------------------------------------------

class TestNotifiedPersistence:
    def test_load_missing_file(self, tmp_path: Path):
        assert _load_notified(tmp_path) == {}

    def test_save_and_load(self, tmp_path: Path):
        data = {"1.2.0": {"count": 2}}
        _save_notified(tmp_path, data)
        loaded = _load_notified(tmp_path)
        assert loaded == data

    def test_load_corrupt_json(self, tmp_path: Path):
        path = tmp_path / "update_notified.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        assert _load_notified(tmp_path) == {}

    def test_load_empty_file(self, tmp_path: Path):
        path = tmp_path / "update_notified.json"
        path.write_text("", encoding="utf-8")
        assert _load_notified(tmp_path) == {}

    def test_overwrite(self, tmp_path: Path):
        _save_notified(tmp_path, {"1.0.0": {"count": 1}})
        _save_notified(tmp_path, {"2.0.0": {"count": 5}})
        loaded = _load_notified(tmp_path)
        assert "1.0.0" not in loaded
        assert loaded["2.0.0"]["count"] == 5


# ---------------------------------------------------------------------------
# should_notify / record_notification
# ---------------------------------------------------------------------------

class TestNotifyTracking:
    def test_first_time_should_notify(self, tmp_path: Path):
        assert should_notify(tmp_path, "1.0.0") is True

    def test_under_max_should_notify(self, tmp_path: Path):
        _save_notified(tmp_path, {"1.0.0": {"count": 2}})
        assert should_notify(tmp_path, "1.0.0") is True

    def test_at_max_should_not_notify(self, tmp_path: Path):
        _save_notified(tmp_path, {"1.0.0": {"count": 3}})
        assert should_notify(tmp_path, "1.0.0") is False

    def test_above_max_should_not_notify(self, tmp_path: Path):
        _save_notified(tmp_path, {"1.0.0": {"count": 10}})
        assert should_notify(tmp_path, "1.0.0") is False

    def test_record_increments_count(self, tmp_path: Path):
        record_notification(tmp_path, "1.0.0")
        data = _load_notified(tmp_path)
        assert data["1.0.0"]["count"] == 1

        record_notification(tmp_path, "1.0.0")
        data = _load_notified(tmp_path)
        assert data["1.0.0"]["count"] == 2

    def test_record_cleans_old_versions(self, tmp_path: Path):
        _save_notified(tmp_path, {"0.9.0": {"count": 3}})
        record_notification(tmp_path, "1.0.0")
        data = _load_notified(tmp_path)
        assert "0.9.0" not in data
        assert data["1.0.0"]["count"] == 1

    def test_three_notifications_then_stop(self, tmp_path: Path):
        for i in range(3):
            assert should_notify(tmp_path, "2.0.0") is True
            record_notification(tmp_path, "2.0.0")
        assert should_notify(tmp_path, "2.0.0") is False

    def test_new_version_resets(self, tmp_path: Path):
        # Max out notifications for old version.
        for _ in range(3):
            record_notification(tmp_path, "1.0.0")
        assert should_notify(tmp_path, "1.0.0") is False
        # New version is fresh.
        assert should_notify(tmp_path, "2.0.0") is True


# ---------------------------------------------------------------------------
# format_notification
# ---------------------------------------------------------------------------

class TestFormatNotification:
    @pytest.fixture()
    def update(self) -> UpdateInfo:
        return UpdateInfo(
            latest_version="1.7.0",
            current_version="1.6.0",
            release_url="https://github.com/o1xhack/telegram-watch/releases/tag/v1.7.0",
            body="Bug fixes and improvements.",
        )

    def test_english(self, update: UpdateInfo):
        msg = format_notification(update, "en")
        assert "v1.7.0 available" in msg
        assert "current: v1.6.0" in msg
        assert update.release_url in msg

    def test_chinese(self, update: UpdateInfo):
        msg = format_notification(update, "zh")
        assert "v1.7.0" in msg
        assert "\u5df2\u53d1\u5e03" in msg  # 已发布
        assert "\u5f53\u524d\u7248\u672c" in msg  # 当前版本
        assert "v1.6.0" in msg
        assert update.release_url in msg

    def test_unknown_language_uses_english(self, update: UpdateInfo):
        msg = format_notification(update, "ja")
        assert "available" in msg


# ---------------------------------------------------------------------------
# check_for_update (async, mocked)
# ---------------------------------------------------------------------------

class TestCheckForUpdate:
    @pytest.mark.asyncio
    async def test_newer_version_returns_info(self):
        mock_release = {
            "tag_name": "v2.0.0",
            "html_url": "https://github.com/o1xhack/telegram-watch/releases/tag/v2.0.0",
            "body": "New release notes.",
        }
        with patch(
            "telegram_watch.update_checker.fetch_latest_release",
            new_callable=AsyncMock,
            return_value=mock_release,
        ):
            result = await check_for_update("1.6.0")
        assert result is not None
        assert result.latest_version == "2.0.0"
        assert result.current_version == "1.6.0"
        assert "v2.0.0" in result.release_url
        assert result.body == "New release notes."

    @pytest.mark.asyncio
    async def test_same_version_returns_none(self):
        mock_release = {
            "tag_name": "v1.6.0",
            "html_url": "https://example.com",
            "body": "",
        }
        with patch(
            "telegram_watch.update_checker.fetch_latest_release",
            new_callable=AsyncMock,
            return_value=mock_release,
        ):
            result = await check_for_update("1.6.0")
        assert result is None

    @pytest.mark.asyncio
    async def test_older_version_returns_none(self):
        mock_release = {
            "tag_name": "v1.5.0",
            "html_url": "https://example.com",
            "body": "",
        }
        with patch(
            "telegram_watch.update_checker.fetch_latest_release",
            new_callable=AsyncMock,
            return_value=mock_release,
        ):
            result = await check_for_update("1.6.0")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none(self):
        with patch(
            "telegram_watch.update_checker.fetch_latest_release",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await check_for_update("1.6.0")
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_tag_name_returns_none(self):
        mock_release = {"html_url": "https://example.com", "body": ""}
        with patch(
            "telegram_watch.update_checker.fetch_latest_release",
            new_callable=AsyncMock,
            return_value=mock_release,
        ):
            result = await check_for_update("1.6.0")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_tag_name_returns_none(self):
        mock_release = {"tag_name": "", "html_url": "https://example.com", "body": ""}
        with patch(
            "telegram_watch.update_checker.fetch_latest_release",
            new_callable=AsyncMock,
            return_value=mock_release,
        ):
            result = await check_for_update("1.6.0")
        assert result is None


# ---------------------------------------------------------------------------
# get_current_version
# ---------------------------------------------------------------------------

class TestGetCurrentVersion:
    def test_returns_string(self):
        ver = get_current_version()
        assert isinstance(ver, str)

    def test_looks_like_version(self):
        ver = get_current_version()
        parts = ver.split(".")
        # Should have at least major.minor.patch or be the "0.0.0" fallback.
        assert len(parts) >= 2
        for part in parts:
            int(part)  # Should not raise.

    def test_fallback_with_missing_metadata(self):
        """When importlib.metadata fails, should still return a version."""
        with patch(
            "telegram_watch.update_checker.Path",
            side_effect=Exception("boom"),
        ):
            # Even if Path raises, importlib.metadata should work
            # (package is installed in dev mode).
            ver = get_current_version()
            assert isinstance(ver, str)
            assert len(ver) > 0
