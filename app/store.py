"""SQLite store: posts, versions, images, audit, outbox (sheet writes), kv, counters, caches.

Every status change goes through `transition()`, which is atomic (compare-and-set in SQL).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

STATUSES = ["Pending", "Processing", "Drafted", "Publishing", "Published", "NeedsFix",
            "Rejected", "Deleted", "Waiting", "Failed"]
TERMINAL = {"Published", "Rejected", "Deleted"}

ALLOWED: dict[str, set[str]] = {
    "Pending": {"Processing", "Deleted"},
    "Processing": {"Drafted", "NeedsFix", "Waiting", "Failed", "Pending"},
    "Drafted": {"Processing", "Publishing", "NeedsFix", "Rejected", "Deleted"},
    "Publishing": {"Published", "Drafted"},
    "Waiting": {"Pending", "Deleted", "Processing"},
    "NeedsFix": {"Pending", "Deleted", "Processing"},
    "Failed": {"Pending", "Deleted"},
    "Published": {"Deleted"},
    "Rejected": set(),
    "Deleted": set(),
}


class InvalidTransition(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    human_hash TEXT NOT NULL DEFAULT '',
    fulltext_hash TEXT NOT NULL DEFAULT '',
    caption_html TEXT NOT NULL DEFAULT '',
    caption_plain TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL DEFAULT '',
    image_mode TEXT NOT NULL DEFAULT 'none',
    image_path TEXT NOT NULL DEFAULT '',
    image_file_id TEXT NOT NULL DEFAULT '',
    stale INTEGER NOT NULL DEFAULT 0,
    silent INTEGER NOT NULL DEFAULT 0,
    channel_message_id INTEGER,
    post_url TEXT NOT NULL DEFAULT '',
    published_at REAL,
    preview_msg_ids TEXT NOT NULL DEFAULT '[]',
    upload_count INTEGER NOT NULL DEFAULT 0,
    edit_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    post_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    caption_html TEXT NOT NULL,
    report_json TEXT NOT NULL,
    human_hash TEXT NOT NULL,
    image_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (post_id, version)
);
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    path TEXT NOT NULL,
    dhash TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    post_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    version INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    column_name TEXT NOT NULL,
    value TEXT NOT NULL,
    row_hint INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_cache (
    url TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    domain TEXT NOT NULL,
    text TEXT NOT NULL,
    ok INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS previews (
    message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (message_id, chat_id)
);
"""


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ posts
    def get_post(self, post_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def create_post(self, post_id: str, status: str = "Pending") -> None:
        t = now()
        self.conn.execute(
            "INSERT OR IGNORE INTO posts (id, status, created_at, updated_at) VALUES (?,?,?,?)",
            (post_id, status, t, t),
        )

    def set_fields(self, post_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE posts SET {cols}, updated_at=? WHERE id=?",
            (*fields.values(), now(), post_id),
        )

    def transition(self, post_id: str, expected: str, to: str, version: int | None = None) -> bool:
        """Atomic compare-and-set. Returns False when the post is no longer in `expected`."""
        if to not in ALLOWED.get(expected, set()):
            raise InvalidTransition(f"{expected} -> {to} مجاز نیست")
        if version is None:
            cur = self.conn.execute(
                "UPDATE posts SET status=?, updated_at=? WHERE id=? AND status=?",
                (to, now(), post_id, expected),
            )
        else:
            cur = self.conn.execute(
                "UPDATE posts SET status=?, updated_at=? WHERE id=? AND status=? AND version=?",
                (to, now(), post_id, expected, version),
            )
        return cur.rowcount == 1

    def posts_with_status(self, status: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM posts WHERE status=?", (status,))]

    def count_status(self, status: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM posts WHERE status=?", (status,)).fetchone()[0])

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM posts GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def recent_texts(self, days: int, exclude_id: str = "") -> list[tuple[str, str]]:
        since = now() - days * 86400
        rows = self.conn.execute(
            "SELECT id, caption_plain FROM posts WHERE updated_at>=? AND caption_plain!='' "
            "AND status IN ('Drafted','Published') AND id!=?",
            (since, exclude_id),
        ).fetchall()
        return [(r["id"], r["caption_plain"]) for r in rows]

    # ------------------------------------------------------------ versions / images
    def add_version(self, post_id: str, version: int, caption_html: str, report_json: str,
                    human_hash: str, image_path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO versions VALUES (?,?,?,?,?,?,?)",
            (post_id, version, caption_html, report_json, human_hash, image_path, now()),
        )

    def add_image(self, post_id: str, path: str, dhash: str, kind: str) -> None:
        self.conn.execute(
            "INSERT INTO images (post_id, path, dhash, kind, created_at) VALUES (?,?,?,?,?)",
            (post_id, path, dhash, kind, now()),
        )

    def recent_dhashes(self, n: int) -> list[str]:
        rows = self.conn.execute("SELECT dhash FROM images ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [r["dhash"] for r in rows]

    # ------------------------------------------------------------ audit
    def audit(self, post_id: str, actor: str, action: str, version: int | None = None, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, post_id, actor, action, version, detail) VALUES (?,?,?,?,?,?)",
            (now(), post_id, actor, action, version, detail[:500]),
        )

    def audit_rows(self, post_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if post_id:
            rows = self.conn.execute("SELECT * FROM audit WHERE post_id=? ORDER BY id DESC LIMIT ?", (post_id, limit))
        else:
            rows = self.conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ outbox (sheet writes)
    def outbox_add(self, post_id: str, column: str, value: str, row_hint: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO outbox (post_id, column_name, value, row_hint, created_at) VALUES (?,?,?,?,?)",
            (post_id, column, value, row_hint, now()),
        )

    def outbox_due(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE next_try<=? ORDER BY id LIMIT ?", (now(), limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def outbox_for(self, post_id: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE post_id=?", (post_id,)).fetchone()[0])

    def outbox_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def outbox_done(self, ids: list[int]) -> None:
        if ids:
            q = ",".join("?" for _ in ids)
            self.conn.execute(f"DELETE FROM outbox WHERE id IN ({q})", ids)

    def outbox_fail(self, ids: list[int], error: str, delay: float) -> None:
        if ids:
            q = ",".join("?" for _ in ids)
            self.conn.execute(
                f"UPDATE outbox SET attempts=attempts+1, last_error=?, next_try=? WHERE id IN ({q})",
                [error[:300], now() + delay, *ids],
            )

    # ------------------------------------------------------------ kv / counters
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))

    def next_seq(self, name: str, at_least: int = 0) -> int:
        """Return the next number for a counter; never goes backwards."""
        row = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        current = int(row["value"]) if row else 0
        nxt = max(current, at_least) + 1
        self.conn.execute("INSERT OR REPLACE INTO counters (name, value) VALUES (?,?)", (name, nxt))
        return nxt

    # ------------------------------------------------------------ source cache
    def cache_get(self, url: str, max_age_s: float) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM source_cache WHERE url=?", (url,)).fetchone()
        if not row or now() - row["fetched_at"] > max_age_s:
            return None
        return dict(row)

    def cache_put(self, url: str, domain: str, text: str, ok: bool, reason: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_cache (url, fetched_at, domain, text, ok, reason) VALUES (?,?,?,?,?,?)",
            (url, now(), domain, text, int(ok), reason),
        )

    # ------------------------------------------------------------ preview message map
    def preview_add(self, message_id: int, chat_id: int, post_id: str, version: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO previews (message_id, chat_id, post_id, version) VALUES (?,?,?,?)",
            (message_id, chat_id, post_id, version),
        )

    def preview_lookup(self, message_id: int, chat_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM previews WHERE message_id=? AND chat_id=?", (message_id, chat_id)
        ).fetchone()
        return dict(row) if row else None

    def previews_for_post(self, post_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM previews WHERE post_id=?", (post_id,)).fetchall()
        return [dict(r) for r in rows]

    def preview_forget_post(self, post_id: str) -> None:
        self.conn.execute("DELETE FROM previews WHERE post_id=?", (post_id,))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default
