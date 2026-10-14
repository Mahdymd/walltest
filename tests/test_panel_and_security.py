import asyncio
from types import SimpleNamespace

from telegram.ext import ConversationHandler

from app import telegram_bot
from app.policy import Report


class Q:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.message = SimpleNamespace(chat_id=111)

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def upd(user_id, q=None, text=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), callback_query=q,
                           effective_message=None)


class Ctx:
    def __init__(self, ctx_obj, bot):
        self.application = SimpleNamespace(bot_data={"ctx": ctx_obj, "notifier": None})
        self.bot = bot
        self.user_data = {}
        self.args = []


def test_callback_data_fits_telegram_limit_and_is_short():
    kb = telegram_bot.preview_keyboard("W-0001", 12)
    for row in kb.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode()) <= 64


def test_non_admin_callback_is_ignored(ctx, fake_bot):
    q = Q("act:W-0001:1:pub")
    called = []

    async def inner(update, context):
        called.append(True)

    wrapped = telegram_bot.admin_only(inner)
    res = asyncio.run(wrapped(upd(999, q), Ctx(ctx, fake_bot)))
    assert res == ConversationHandler.END and called == []


def test_admin_callback_passes(ctx, fake_bot):
    called = []

    async def inner(update, context):
        called.append(True)
        return 0

    wrapped = telegram_bot.admin_only(inner)
    asyncio.run(wrapped(upd(111, Q("x")), Ctx(ctx, fake_bot)))
    assert called == [True]


def test_stale_version_button_does_nothing(ctx, store, fake_bot):
    store.create_post("W-0001")
    store.set_fields("W-0001", status="Drafted", version=2, report_json=Report().to_json())
    q = Q("act:W-0001:1:pub")
    upd_obj = upd(111, q)
    asyncio.run(telegram_bot.on_act(upd_obj, Ctx(ctx, fake_bot)))
    assert q.answers and "قدیمی" in (q.answers[0][0] or "")
    assert fake_bot.sent == []


def test_block_verdict_has_no_publish_path(ctx, store, fake_bot):
    rep = Report()
    from app.policy import Finding
    rep.findings.append(Finding("BLOCK", "numbers", "عدد بدون منبع", "40"))
    store.create_post("W-0002")
    store.set_fields("W-0002", status="Drafted", version=1, report_json=rep.to_json())
    q = Q("act:W-0002:1:pub")
    asyncio.run(telegram_bot.on_act(upd(111, q), Ctx(ctx, fake_bot)))
    assert q.answers[0][0] and "قابل انتشار نیست" in q.answers[0][0]
    assert fake_bot.sent == []


def test_review_requires_confirmation_before_publish(ctx, store, fake_bot):
    rep = Report()
    from app.policy import Finding
    rep.findings.append(Finding("REVIEW", "hashtags", "تعداد هشتگ 0"))
    store.create_post("W-0003")
    store.set_fields("W-0003", status="Drafted", version=1, report_json=rep.to_json())
    q = Q("act:W-0003:1:pub")
    asyncio.run(telegram_bot.on_act(upd(111, q), Ctx(ctx, fake_bot)))
    assert any("بررسی" in s["text"] for s in fake_bot.sent)
    assert store.get_post("W-0003")["status"] == "Drafted"  # nothing published yet
