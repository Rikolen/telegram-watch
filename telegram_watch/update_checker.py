"""Automatic update checker -- queries GitHub Releases API."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com/repos/o1xhack/telegram-watch/releases/latest"
_NOTIFIED_FILE = "update_notified.json"
_MAX_NOTIFICATIONS = 3
_TIMEOUT = 10


@dataclass
class UpdateInfo:
    latest_version: str   # e.g. "1.7.0"
    current_version: str  # e.g. "1.6.0"
    release_url: str
    body: str             # release body text


def get_current_version() -> str:
    """Read version from package metadata or pyproject.toml."""
    # Try importlib.metadata first (works when package is installed).
    try:
        from importlib.metadata import version
        return version("telegram-watch")
    except Exception:
        pass
    # Fallback: read pyproject.toml relative to this file.
    try:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("version"):
                    # Parse  version = "x.y.z"
                    _, _, val = stripped.partition("=")
                    return val.strip().strip('"').strip("'")
    except Exception:
        pass
    return "0.0.0"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' or '1.2.3' into (1, 2, 3)."""
    return tuple(int(x) for x in v.lstrip("v").split("."))


def _is_newer(remote: str, local: str) -> bool:
    """Return True if remote version is strictly newer than local."""
    try:
        return _parse_version(remote) > _parse_version(local)
    except (ValueError, TypeError):
        return False


def _fetch_latest_release_sync() -> dict | None:
    """Blocking HTTP fetch of the latest GitHub release. Returns None on failure."""
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "telegram-watch-update-checker",
            },
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
        return None


async def fetch_latest_release() -> dict | None:
    """Fetch latest release from GitHub API. Returns None on any failure.

    Runs the blocking HTTP call in an executor so the async loop is not blocked.
    """
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, _fetch_latest_release_sync
        )
    except Exception as exc:
        logger.debug("Update check executor error: %s", exc)
        return None


def _load_notified(data_dir: Path) -> dict:
    """Load notification tracking from JSON file."""
    path = data_dir / _NOTIFIED_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_notified(data_dir: Path, data: dict) -> None:
    """Save notification tracking to JSON file."""
    path = data_dir / _NOTIFIED_FILE
    path.write_text(json.dumps(data), encoding="utf-8")


def should_notify(data_dir: Path, version: str) -> bool:
    """Return True if we haven't yet notified _MAX_NOTIFICATIONS times for this version."""
    data = _load_notified(data_dir)
    entry = data.get(version, {})
    return entry.get("count", 0) < _MAX_NOTIFICATIONS


def record_notification(data_dir: Path, version: str) -> None:
    """Increment the notification count for a version.

    Cleans old versions -- only the latest notified version is kept.
    """
    data = _load_notified(data_dir)
    entry = data.get(version, {"count": 0})
    entry["count"] = entry.get("count", 0) + 1
    # Clean old versions -- only keep the latest.
    data = {version: entry}
    _save_notified(data_dir, data)


def format_notification(update: UpdateInfo, language: str) -> str:
    """Format update notification message based on language."""
    if language == "zh":
        return (
            f"\U0001f195 telegram-watch v{update.latest_version} \u5df2\u53d1\u5e03"
            f"\uff08\u5f53\u524d\u7248\u672c\uff1av{update.current_version}\uff09\n\n"
            f"{update.release_url}"
        )
    return (
        f"\U0001f195 telegram-watch v{update.latest_version} available"
        f" (current: v{update.current_version})\n\n"
        f"{update.release_url}"
    )


async def check_for_update(current_version: str) -> UpdateInfo | None:
    """Check GitHub for a newer release. Returns UpdateInfo or None."""
    release = await fetch_latest_release()
    if release is None:
        return None
    tag = release.get("tag_name", "")
    if not tag:
        return None
    remote_ver = tag.lstrip("v")
    if not _is_newer(remote_ver, current_version):
        return None
    return UpdateInfo(
        latest_version=remote_ver,
        current_version=current_version,
        release_url=release.get("html_url", ""),
        body=release.get("body", ""),
    )
