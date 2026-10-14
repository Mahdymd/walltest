"""Automatic topics queue: data/inbox/topics.txt -> one post per interval, approval still manual."""
import asyncio

import pytest

from app import geministate, pipeline, topics
from app.pipeline import Outcome


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    p = {"topics": tmp_path / "topics.txt", "done": tmp_path / "done.txt", "failed": tmp_path / "failed.txt"}
    monkeypatch.setattr(topics, "TOPICS_PATH", p["topics"])
    monkeypatch.setattr(topics, "DONE_PATH", p["done"])
    monkeypatch.setattr(topics, "FAILED_PATH", p["failed"])
    return p


def test_add_and_take_in_order_ignoring_comments_and_blanks(paths):
    assert topics.add_topic("اولین موضوع پست") == 1
    paths["topics"].write_text(paths["topics"].read_text(encoding="utf-8") + "# یادداشت\n\n", encoding="utf-8")
    assert topics.add_topic("دومین   موضوع\tپست") == 2
    assert topics.queued() == ["اولین موضوع پست", "دومین موضوع پست"]
    assert topics.take_next() == "اولین موضوع پست"
    assert topics.queued() == ["دومین موضوع پست"]
    assert topics.take_next() == "دومین موضوع پست"
    assert topics.take_next() is None


def test_bad_topics_are_refused(paths):
    with pytest.raises(ValueError):
        topics.add_topic("کم")
    with pytest.raises(ValueError):
        topics.add_topic("x" * 301)
    assert topics.queued() == []


def test_empty_queue_is_idle(ctx, rec, paths):
    assert run(topics.run_next(ctx, rec)) == "idle"


def test_disabled_queue_does_nothing(ctx, rec, paths):
    topics.add_topic("موضوع آزمایشی برای صف")
    ctx.cfg.topics.enabled = False
    assert run(topics.run_next(ctx, rec)) == "idle"
    assert topics.queued() == ["موضوع آزمایشی برای صف"]


def test_paused_keeps_the_topic_in_queue(ctx, rec, paths, store):
    topics.add_topic("موضوع آزمایشی برای صف")
    geministate.set_paused(store, True)
    assert run(topics.run_next(ctx, rec)) == "paused"
    assert topics.queued() == ["موضوع آزمایشی برای صف"]


def test_waits_when_enough_drafts_are_waiting_for_review(ctx, rec, paths, backend):
    topics.add_topic("موضوع آزمایشی برای صف")
    ctx.cfg.sheet.max_pending_drafts = 1
    from app.sheets import COLUMNS
    row = [""] * len(COLUMNS)
    row[COLUMNS.index("ID")] = "W-0001"
    row[COLUMNS.index("Status")] = "Drafted"
    backend.tabs["Content"].append(row)
    assert run(topics.run_next(ctx, rec)) == "waiting"
    assert topics.queued() == ["موضوع آزمایشی برای صف"]


def test_success_is_logged_and_removed(ctx, rec, paths, monkeypatch):
    topics.add_topic("موضوع موفق برای تست صف")

    async def fake_new_post(ctx_, notifier, brief):  # noqa: ANN001
        assert brief == "موضوع موفق برای تست صف"
        return Outcome("W-0007", "Drafted")
    monkeypatch.setattr(pipeline, "new_post_from_brief", fake_new_post)
    assert run(topics.run_next(ctx, rec)) == "done"
    assert topics.queued() == []
    assert "W-0007 Drafted" in paths["done"].read_text(encoding="utf-8")


def test_failure_is_logged_and_admin_is_told(ctx, rec, paths, monkeypatch):
    topics.add_topic("موضوعی که شکست می‌خورد")

    async def failing(ctx_, notifier, brief):  # noqa: ANN001
        return Outcome("", "Failed", message="Gemini درخواست را رد کرد")
    monkeypatch.setattr(pipeline, "new_post_from_brief", failing)
    assert run(topics.run_next(ctx, rec)) == "failed"
    assert topics.queued() == []
    assert "رد کرد" in paths["failed"].read_text(encoding="utf-8")
    assert "alert" in rec.kinds()


def test_structured_refusal_is_retried_with_plain_prompt(ctx, rec, backend, monkeypatch):
    from app import gemini, gemini_post
    from app.gemini import Selectors
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: Selectors(
        "verified", {"prompt_box": "a", "send_button": "b", "image_result": "c", "response_text": "d"}))
    monkeypatch.setattr(gemini_post, "check_ready", lambda ctx_: None)
    seen = []

    def write(brief, timeout_s, variant="structured"):  # noqa: ANN001
        seen.append(variant)
        if variant == "structured":
            return "نمی‌توانم کمکی کنم، من فقط مدل زبان هستم."
        return "انرژی خورشیدی برای خانه‌های کوچک هم مناسب است. #انرژی #پایدار"
    monkeypatch.setattr(gemini_post, "write_post_blocking", write)
    out = run(pipeline.new_post_from_brief(ctx, rec, "انرژی خورشیدی"))
    assert seen == ["structured", "plain"]  # exactly one retry with the plain wording
    assert out.status != "Failed", out.message
    assert out.post_id == "W-0001"
    assert backend.tabs["Content"][1][0] == "W-0001"  # the row was created from the plain answer


def test_two_refusals_give_a_clear_failure_and_no_row(ctx, rec, backend, monkeypatch):
    from app import gemini, gemini_post
    from app.gemini import Selectors
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: Selectors(
        "verified", {"prompt_box": "a", "send_button": "b", "image_result": "c", "response_text": "d"}))
    monkeypatch.setattr(gemini_post, "check_ready", lambda ctx_: None)
    monkeypatch.setattr(gemini_post, "write_post_blocking",
                        lambda brief, timeout_s, variant="structured": "نمی‌توانم کمکی کنم، من فقط مدل زبان هستم.")
    out = run(pipeline.new_post_from_brief(ctx, rec, "موضوع دشوار برای امتحان"))
    assert out.status == "Failed"
    assert "اصلاح" in out.message or "بازنویسی" in out.message
    assert len(backend.tabs["Content"]) == 1  # header only: no row was created
