"""Gemini budget, pause flag, restart cadence and watermark state (SQLite kv, no browser)."""
from datetime import datetime, timezone

from app import geministate
from app.store import Store


def test_day_key_uses_tehran_time_not_utc():
    # 22:00 UTC is already the next day in Tehran (UTC+3:30)
    assert geministate.today_key(datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc)) == "2026-10-14"


def test_daily_budget_counts_and_rolls_over(tmp_path):
    store = Store(tmp_path / "s.db")
    day1 = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    for _ in range(3):
        geministate.begin_attempt(store, 20, now=day1)
    assert geministate.used_today(store, now=day1) == 3
    assert geministate.daily_left(store, 3, now=day1) == 0
    assert geministate.daily_left(store, 3, now=day2) == 3  # new day, fresh budget
    store.close()


def test_restart_every_n_images_starts_new_chat_on_schedule(tmp_path):
    store = Store(tmp_path / "s.db")
    flags = [geministate.begin_attempt(store, 3) for _ in range(5)]
    assert flags == [False, False, False, True, False]
    store.close()


def test_restart_zero_disables_new_chat(tmp_path):
    store = Store(tmp_path / "s.db")
    assert not any(geministate.begin_attempt(store, 0) for _ in range(5))
    store.close()


def test_pause_flag_round_trip(tmp_path):
    store = Store(tmp_path / "s.db")
    assert not geministate.is_paused(store)
    geministate.set_paused(store, True)
    assert geministate.is_paused(store)
    geministate.set_paused(store, False)
    assert not geministate.is_paused(store)
    store.close()


def test_watermark_label_follows_probe_answer(tmp_path):
    store = Store(tmp_path / "s.db")
    assert geministate.watermark_label(store) == geministate.WATERMARK_UNKNOWN
    geministate.set_watermark(store, True)
    assert "دارد" in geministate.watermark_label(store) and "ندارد" not in geministate.watermark_label(store)
    geministate.set_watermark(store, False)
    assert "ندارد" in geministate.watermark_label(store)
    store.close()


def test_policy_report_uses_probe_result(ctx, store):
    from app.pipeline import evaluate_row
    geministate.set_watermark(store, False)
    ev = evaluate_row(ctx, "W-0100", {"Title": "سلام", "Body": "متن ساده", "Hashtags": "#تست",
                                      "ImageMode": "none"}, None, [], "")
    assert "ندارد" in ev.report.watermark
