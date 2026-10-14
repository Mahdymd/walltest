"""Retention pruning and daily database backup."""
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app import housekeeping
from app.housekeeping import backup_database, prune_older_than

TEHRAN = ZoneInfo("Asia/Tehran")


def _touch(path, age_days, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    t = now - age_days * 86400
    os.utime(path, (t, t))


def test_prune_removes_only_old_files(tmp_path):
    now = time.time()
    old = tmp_path / "a" / "old.jpg"
    new = tmp_path / "a" / "new.jpg"
    _touch(old, 40, now)
    _touch(new, 1, now)
    assert prune_older_than(tmp_path, 30, now=now) == 1
    assert not old.exists() and new.exists()


def test_prune_zero_days_disables_pruning(tmp_path):
    now = time.time()
    f = tmp_path / "old.txt"
    _touch(f, 400, now)
    assert prune_older_than(tmp_path, 0, now=now) == 0
    assert f.exists()


def test_prune_missing_dir_is_noop(tmp_path):
    assert prune_older_than(tmp_path / "nope", 1) == 0


def test_backup_written_once_per_day(tmp_path):
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()
    backups = tmp_path / "backups"
    now = datetime(2026, 10, 12, 10, 0, tzinfo=TEHRAN)
    first = backup_database(db, backups, now=now)
    assert first is not None and first.name == "wal-20261012.db"
    assert backup_database(db, backups, now=now) is None  # same day: skipped
    copy = sqlite3.connect(first)
    assert copy.execute("SELECT x FROM t").fetchone() == (7,)
    copy.close()


def test_backup_skips_missing_db(tmp_path):
    assert backup_database(tmp_path / "missing.db", tmp_path / "b") is None


def test_run_housekeeping_isolates_failures(monkeypatch, tmp_path):
    from app.config import Config

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(housekeeping, "prune_older_than", boom)
    monkeypatch.setattr(housekeeping, "backup_database", lambda *a, **k: None)
    result = housekeeping.run_housekeeping(Config(), tmp_path / "none.db")
    assert result["backup_written"] == 0
