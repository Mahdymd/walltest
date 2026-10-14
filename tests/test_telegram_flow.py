"""End-to-end admin flow with a fake Telegram bot: sheet row -> preview -> publish -> edit by reply."""
import asyncio
from types import SimpleNamespace

from app import pipeline, telegram_bot
from app.policy import SourceResult
from app.sheets import COLUMNS
from tests.conftest import FakeBot, make_row


class Q:
    def __init__(self, data, chat_id=111):
        self.data = data
        self.message = SimpleNamespace(chat_id=chat_id, delete=self._delete)
        self.answers = []

    async def _delete(self):
        return None

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


class Msg:
    def __init__(self, text="", message_id=1, reply_to=None):
        self.text = text
        self.text_html = text
        self.message_id = message_id
        self.reply_to_message = reply_to
        self.photo = []
        self.document = None
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return Msg(text, message_id=900 + len(self.replies))


class Ctx:
    def __init__(self, app_ctx, bot):
        self.notifier = telegram_bot.TgNotifier(bot, app_ctx)
        self.application = SimpleNamespace(bot_data={"ctx": app_ctx, "notifier": self.notifier})
        self.bot = bot
        self.user_data = {}
        self.args = []


def upd(user_id=111, q=None, msg=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), callback_query=q,
                           effective_message=msg)


def test_full_admin_flow(ctx, backend, store, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_source", lambda *a, **k: SourceResult(url="", domain="", ok=False))
    bot = FakeBot()
    cx = Ctx(ctx, bot)
    backend.tabs["Content"].append(make_row(ID="W-0500", Status="Pending", Title="سلام دوستان",
                                            Body="این یک متن آزمایشی است.", Hashtags="#تست", ImageMode="none"))
    asyncio.run(pipeline.sync_once(ctx, cx.notifier))
    post = store.get_post("W-0500")
    assert post["status"] == "Drafted" and post["version"] == 1
    preview = [m for m in bot.sent if "caption" not in m and "reply_markup" in m]
    assert preview, "پیش‌نمایش برای ادمین ارسال نشد"
    assert all(m["chat_id"] == 111 for m in bot.sent), "هیچ پیامی جز به ادمین نباید برود"

    # publish with a click
    q = Q("act:W-0500:1:pub")
    asyncio.run(telegram_bot.on_act(upd(111, q), cx))
    channel_sends = [m for m in bot.sent if m["chat_id"] == "@waltest0001"]
    assert len(channel_sends) == 1
    assert store.get_post("W-0500")["status"] == "Published"
    assert backend.tabs["Content"][1][COLUMNS.index("Status")] in ("Processing", "Drafted", "Published")

    # a stale button after publish does nothing new
    q2 = Q("act:W-0500:1:pub")
    asyncio.run(telegram_bot.on_act(upd(111, q2), cx))
    assert len([m for m in bot.sent if m["chat_id"] == "@waltest0001"]) == 1


def test_reply_text_edits_draft_and_creates_new_version(ctx, backend, store, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_source", lambda *a, **k: SourceResult(url="", domain="", ok=False))
    bot = FakeBot()
    cx = Ctx(ctx, bot)
    backend.tabs["Content"].append(make_row(ID="W-0501", Status="Pending", Title="a", Body="متن اول",
                                            Hashtags="#تست", ImageMode="none"))
    asyncio.run(pipeline.sync_once(ctx, cx.notifier))
    first_preview = next(m for m in bot.sent if m.get("reply_markup") is not None)
    # the preview message id is what the admin replies to
    preview_rows = store.previews_for_post("W-0501")
    mid = min(r["message_id"] for r in preview_rows)  # the preview itself (the report is a reply to it)
    target = SimpleNamespace(message_id=mid)
    row = store.preview_lookup(mid, 111)
    assert row is not None and row["post_id"] == "W-0501"
    reply = Msg("متن <b>ویرایش‌شده</b>", message_id=2000, reply_to=target)
    asyncio.run(telegram_bot.on_reply_shortcut(upd(111, msg=reply), cx))
    assert store.get_post("W-0501")["version"] == 2
    assert "ویرایش‌شده" in store.get_post("W-0501")["caption_html"]
    assert first_preview is not None


def test_non_admin_reply_is_ignored(ctx, backend, store):
    bot = FakeBot()
    cx = Ctx(ctx, bot)
    reply = Msg("hi", reply_to=SimpleNamespace(message_id=1))
    asyncio.run(telegram_bot.on_reply_shortcut(upd(999, msg=reply), cx))
    assert bot.sent == []
