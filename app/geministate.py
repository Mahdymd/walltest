"""Small persistent state for Gemini image generation, kept in the SQLite kv table.

- pause flag (/pause, /resume): stops automatic generation only; publishing is not affected
- daily budget (gemini.max_images_per_day), day boundaries in Asia/Tehran
- 'new chat every N images' counter (gemini.restart_every_images)
- watermark result from the /testimage probe (the admin answers yes/no once)
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
WATERMARK_UNKNOWN = "نامعلوم (با /testimage بررسی کنید)"


def today_key(now: datetime | None = None) -> str:
    return (now or datetime.now(TEHRAN)).astimezone(TEHRAN).strftime("%Y-%m-%d")


def is_paused(store) -> bool:  # noqa: ANN001
    return store.kv_get("gemini_paused", "0") == "1"


def set_paused(store, paused: bool) -> None:  # noqa: ANN001
    store.kv_set("gemini_paused", "1" if paused else "0")


def used_today(store, now: datetime | None = None) -> int:  # noqa: ANN001
    if store.kv_get("gemini_day", "") != today_key(now):
        return 0
    return int(store.kv_get("gemini_day_used", "0") or 0)


def daily_left(store, limit: int, now: datetime | None = None) -> int:  # noqa: ANN001
    return max(0, limit - used_today(store, now))


def begin_attempt(store, restart_every: int, now: datetime | None = None) -> bool:  # noqa: ANN001
    """Record one generation attempt. Returns True when it must start in a new Gemini chat."""
    since = int(store.kv_get("gemini_since_restart", "0") or 0)
    new_chat = restart_every > 0 and since >= restart_every
    if new_chat:
        since = 0
    store.kv_set("gemini_since_restart", str(since + 1))
    day = today_key(now)
    store.kv_set("gemini_day", day)
    store.kv_set("gemini_day_used", str(used_today(store, now) + 1))
    return new_chat


def watermark_label(store) -> str:  # noqa: ANN001
    value = store.kv_get("gemini_watermark", "")
    if value == "yes":
        return "دارد (تأیید ادمین با /testimage)"
    if value == "no":
        return "ندارد (تأیید ادمین با /testimage)"
    return WATERMARK_UNKNOWN


def set_watermark(store, present: bool) -> None:  # noqa: ANN001
    store.kv_set("gemini_watermark", "yes" if present else "no")


def begin_text_attempt(store, limit: int, now: datetime | None = None) -> bool:  # noqa: ANN001
    """Count one Gemini text request for today. Returns False when the daily text limit is reached."""
    day = today_key(now)
    used = int(store.kv_get("gemini_text_used", "0") or 0) if store.kv_get("gemini_text_day", "") == day else 0
    if used >= limit:
        return False
    store.kv_set("gemini_text_day", day)
    store.kv_set("gemini_text_used", str(used + 1))
    return True
