"""Startup housekeeping: daily SQLite backup and retention pruning (config.retention).

Nothing here touches the sheet, Telegram, or the channel. It only manages local files.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

from .config import ARTIFACTS_DIR, BACKUPS_DIR, IMAGES_DIR, Config

TEHRAN = ZoneInfo("Asia/Tehran")

log = logging.getLogger("wal.housekeeping")

DAY_S = 86400


def prune_older_than(root: Path, days: int, now: float | None = None, pattern: str = "*") -> int:
    """Delete files under root whose mtime is older than `days`. Returns the number removed."""
    if days <= 0 or not root.exists():
        return 0
    cutoff = (time.time() if now is None else now) - days * DAY_S
    removed = 0
    for path in root.rglob(pattern):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not prune %s: %s", path.name, exc)
    return removed


def backup_database(db_path: Path, backups_dir: Path, now: datetime | None = None) -> Path | None:
    """Write one backup per Tehran day using the SQLite online backup API. Skips if today's exists."""
    if not db_path.exists():
        return None
    stamp = (now or datetime.now(TEHRAN)).strftime("%Y%m%d")
    target = backups_dir / f"wal-{stamp}.db"
    if target.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    log.info("database backup written: %s", target.name)
    return target


def run_housekeeping(cfg: Config, db_path: Path, now: float | None = None) -> dict[str, int]:
    """Run all startup housekeeping steps. Each step is isolated so one failure does not stop others."""
    result: dict[str, int] = {"images": 0, "artifacts": 0, "backups": 0, "backup_written": 0}
    r = cfg.retention
    try:
        result["images"] = prune_older_than(IMAGES_DIR, r.raw_images_days, now)
        result["artifacts"] = prune_older_than(ARTIFACTS_DIR, r.artifacts_days, now)
        result["backups"] = prune_older_than(BACKUPS_DIR, r.backups_days, now, pattern="wal-*.db")
    except Exception:  # noqa: BLE001
        log.exception("retention pruning failed")
    try:
        if backup_database(db_path, BACKUPS_DIR) is not None:
            result["backup_written"] = 1
    except Exception:  # noqa: BLE001
        log.exception("database backup failed")
    return result
