import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from app import pipeline, publisher
from app.sheets import COLUMNS
from tests.conftest import make_row


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def drafted(ctx, backend, store, rec, monkeypatch):
    from app.policy import SourceResult

    monkeypatch.setattr(pipeline, "fetch_source", lambda *a, **k: SourceResult(url="", domain="", ok=False))
    backend.tabs["Content"].append(make_row(ID="W-0100", Status="Pending", Title="سلام", Body="متن", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    run(pipeline.flush_now(ctx))
    return ctx


class CountingBot:
    def __init__(self, fail_network=False):
        self.sends = []
        self.fail_network = fail_network

    async def send_message(self, chat_id, text, **kw):
        if self.fail_network:
            raise NetworkError("connection reset")
        self.sends.append((chat_id, text))
        await asyncio.sleep(0)
        return SimpleNamespace(message_id=len(self.sends) + 500)

    async def send_photo(self, chat_id, photo, **kw):
        return await self.send_message(chat_id, kw.get("caption", ""))

    async def get_chat(self, chat_id):
        return SimpleNamespace(username="waltest0001")


def test_double_publish_sends_exactly_once(drafted):
    bot = CountingBot()

    async def go():
        return await asyncio.gather(
            publisher.publish(bot, drafted, "W-0100", 1, False, "admin:111"),
            publisher.publish(bot, drafted, "W-0100", 1, False, "admin:111"),
        )
    results = run(go())
    assert len(bot.sends) == 1
    kinds = sorted(r.kind for r in results)
    assert kinds == ["already", "ok"]
    assert drafted.store.get_post("W-0100")["status"] == "Published"


def test_channel_message_goes_to_channel_id(drafted):
    bot = CountingBot()
    run(publisher.publish(bot, drafted, "W-0100", 1, True, "admin:111"))
    assert bot.sends[0][0] == "@waltest0001"


def test_stale_version_refused(drafted):
    bot = CountingBot()
    out = run(publisher.publish(bot, drafted, "W-0100", 99, False, "admin:111"))
    assert out.kind == "stale" and bot.sends == []


def test_sheet_changed_before_publish_refused(drafted, backend):
    backend.tabs["Content"][1][COLUMNS.index("Body")] = "تغییر دستی"
    bot = CountingBot()
    out = run(publisher.publish(bot, drafted, "W-0100", 1, False, "admin:111"))
    assert out.kind == "stale" and bot.sends == []
    assert drafted.store.get_post("W-0100")["stale"] == 1


def test_network_cut_keeps_publishing_and_asks(drafted):
    bot = CountingBot(fail_network=True)
    out = run(publisher.publish(bot, drafted, "W-0100", 1, False, "admin:111"))
    assert out.kind == "unknown"
    assert drafted.store.get_post("W-0100")["status"] == "Publishing"


def test_recovery_yes_marks_published_without_resend(drafted):
    bot = CountingBot(fail_network=True)
    run(publisher.publish(bot, drafted, "W-0100", 1, False, "admin:111"))
    ok_bot = CountingBot()
    out = run(publisher.resolve_unknown(ok_bot, drafted, "W-0100", 1, True, False, "admin:111"))
    assert out.kind == "ok" and ok_bot.sends == []
    assert drafted.store.get_post("W-0100")["status"] == "Published"


def test_recovery_no_resends_once(drafted):
    bot = CountingBot(fail_network=True)
    run(publisher.publish(bot, drafted, "W-0100", 1, False, "admin:111"))
    ok_bot = CountingBot()
    out = run(publisher.resolve_unknown(ok_bot, drafted, "W-0100", 1, False, False, "admin:111"))
    assert out.kind == "ok" and len(ok_bot.sends) == 1


def test_rejected_post_cannot_publish(drafted):
    drafted.store.transition("W-0100", "Drafted", "Rejected", version=1)
    out = run(publisher.publish(CountingBot(), drafted, "W-0100", 1, False, "admin:111"))
    assert out.kind == "already"


def test_persian_errors_are_helpful():
    from telegram.error import BadRequest
    assert "ادمین" in publisher.persian_error(BadRequest("Chat not found")) or "کانال" in publisher.persian_error(BadRequest("Chat not found"))
    assert "دسترسی" in publisher.persian_error(BadRequest("not enough rights to send text messages"))
