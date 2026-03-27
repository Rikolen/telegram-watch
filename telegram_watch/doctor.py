"""Doctor command implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from rich.console import Console
from rich.table import Table

from .config import Config
from .storage import db_session


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warn: bool = False


def run_doctor(config: Config) -> None:
    """Validate config and local environment."""
    console = Console()
    checks: list[CheckResult] = []
    checks.append(CheckResult("config", True, "config parsed successfully"))

    checks.extend(
        [
            _check_dir("session dir", config.telegram.session_file.parent),
            _check_dir("media dir", config.storage.media_dir),
            _check_dir("reports dir", config.reporting.reports_dir),
        ]
    )
    if config.sender is not None:
        checks.append(_check_dir("sender session dir", config.sender.session_file.parent))

    checks.extend(_check_cloud_sync("session file", config.telegram.session_file))
    checks.extend(_check_cloud_sync("database", config.storage.db_path))

    checks.append(_check_db(config))

    table = Table(title="telegram-watch doctor", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")
    success = True
    for result in checks:
        if result.warn:
            status = "[yellow]WARN[/yellow]"
        elif result.ok:
            status = "[green]OK[/green]"
        else:
            status = "[red]FAIL[/red]"
        table.add_row(result.name, status, result.detail)
        if not result.ok and not result.warn:
            success = False

    console.print(table)
    if not success:
        raise SystemExit("doctor failed")


def _check_dir(label: str, path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(label, False, f"cannot create {path}: {exc}")
    return CheckResult(label, True, f"{path}")


_CLOUD_SYNC_PATTERNS: dict[str, tuple[str, ...]] = {
    "Dropbox": ("/Dropbox/",),
    "iCloud": ("/Library/Mobile Documents/", "/iCloud/"),
    "OneDrive": ("/OneDrive/",),
    "Google Drive": ("/Google Drive/", "/GoogleDrive/"),
}


def _check_cloud_sync(label: str, path: Path) -> list[CheckResult]:
    """Warn if *path* sits inside a known cloud sync directory."""
    results: list[CheckResult] = []
    abs_path = str(path.resolve())
    for service, markers in _CLOUD_SYNC_PATTERNS.items():
        if any(marker in abs_path for marker in markers):
            results.append(
                CheckResult(
                    name=f"cloud sync ({label})",
                    ok=True,
                    detail=(
                        f"{label} is inside a cloud sync directory ({service}). "
                        "Cloud sync can cause SQLite lock conflicts. Consider moving "
                        "data files outside the sync folder, or ensure WAL mode is "
                        "enabled (default since v1.6.1)."
                    ),
                    warn=True,
                )
            )
    return results


def _check_db(config: Config) -> CheckResult:
    try:
        with db_session(config.storage.db_path) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - surfaces in console only
        return CheckResult("database", False, f"{exc}")
    return CheckResult("database", True, f"{config.storage.db_path}")
