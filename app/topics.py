"""Automatic posts from a plain text queue: data/inbox/topics.txt (one topic per line).

Every interval the bot takes the first topic, writes the post with Gemini, adds a row to the
local queue and sends the normal preview to the admin. Publishing still needs the admin's approval.
Taken topics are logged to done.txt or failed.txt, so nothing is lost silently.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import geministate, pipeline
from .config import INBOX_DIR
from .context import AppContext

log = logging.getLogger(__name__)

TOPICS_PATH = INBOX_DIR / "topics.txt"
DONE_PATH = INBOX_DIR / "done.txt"
FAILED_PATH = INBOX_DIR / "failed.txt"
_lock = threading.Lock()


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8-sig").splitlines()


def _is_topic(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def queued(path: Path | None = None) -> list[str]:
    path = path or TOPICS_PATH
    with _lock:
        return [ln.strip() for ln in _lines(path) if _is_topic(ln)]


def add_topic(text: str, path: Path | None = None) -> int:
    """Append one topic. Returns the queue length after adding. Raises ValueError for bad input."""
    topic = " ".join((text or "").split())
    if len(topic) < 5:
        raise ValueError("موضوع خیلی کوتاه است (حداقل ۵ حرف).")
    if len(topic) > 300:
        raise ValueError("موضوع خیلی بلند است (حداکثر ۳۰۰ حرف).")
    path = path or TOPICS_PATH
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _lines(path)
        existing.append(topic)
        path.write_text("\n".join(existing) + "\n", encoding="utf-8")
    return len(queued(path))


def take_next(path: Path | None = None) -> str | None:
    """Remove and return the first topic from the queue."""
    path = path or TOPICS_PATH
    with _lock:
        lines = _lines(path)
        for i, ln in enumerate(lines):
            if _is_topic(ln):
                topic = ln.strip()
                del lines[i]
                path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                return topic
    return None


def _log(path: Path, topic: str, note: str) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} | {topic} | {note}\n")


def _drafted_count(ctx: AppContext) -> int:
    snap = ctx.sheet.read_content()
    return sum(1 for r in snap.rows if r.status == "Drafted")


async def run_next(ctx: AppContext, notifier) -> str:  # noqa: ANN001
    """One scheduler tick. Returns a short reason: idle, paused, waiting, done or failed."""
    if not ctx.cfg.topics.enabled:
        return "idle"
    if not queued():
        return "idle"
    if geministate.is_paused(ctx.store):
        return "paused"
    try:
        if _drafted_count(ctx) >= ctx.cfg.sheet.max_pending_drafts:
            return "waiting"  # admin has enough previews to review
    except Exception as exc:  # noqa: BLE001
        log.warning("topics: queue read failed: %s", str(exc)[:200])
        return "idle"
    topic = take_next()
    if topic is None:
        return "idle"
    out = await pipeline.new_post_from_brief(ctx, notifier, topic)
    if out.status == "Failed" and not out.post_id:
        _log(FAILED_PATH, topic, out.message or "خطای نامشخص")
        await notifier.alert(f"❌ موضوع «{topic[:60]}» پست نشد: {out.message}\n"
                             "برای تلاش دوباره با /topic دوباره اضافه کنید.")
        return "failed"
    _log(DONE_PATH, topic, f"{out.post_id} {out.status}")
    return "done"
