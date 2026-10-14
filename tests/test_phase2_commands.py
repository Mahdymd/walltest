"""Telegram commands for phase 2, called directly with fake updates (no network)."""
import asyncio
from types import SimpleNamespace

from app import geministate, telegram_bot


class Msg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)


def _update_context(ctx, user_id=111):
    msg = Msg()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=msg,
                             callback_query=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": ctx}))
    return update, context, msg


def test_pause_and_resume_commands(ctx, store):
    update, context, msg = _update_context(ctx)
    asyncio.run(telegram_bot.cmd_pause(update, context))
    assert geministate.is_paused(store)
    assert "متوقف" in msg.texts[-1]
    asyncio.run(telegram_bot.cmd_resume(update, context))
    assert not geministate.is_paused(store)


def test_non_admin_cannot_pause(ctx, store):
    update, context, msg = _update_context(ctx, user_id=999)
    asyncio.run(telegram_bot.cmd_pause(update, context))
    assert not geministate.is_paused(store)
    assert msg.texts == []


def test_testimage_refused_with_reason_when_gate_closed(ctx, store, monkeypatch):
    from app import gemini
    from app.gemini import Selectors
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: Selectors("needs_verification", {}))
    update, context, msg = _update_context(ctx)
    asyncio.run(telegram_bot.cmd_testimage(update, context))
    assert "تأیید نشده" in msg.texts[-1]


def test_status_shows_gemini_and_watermark_lines(ctx, store):
    update, context, msg = _update_context(ctx)
    asyncio.run(telegram_bot.cmd_status(update, context))
    text = msg.texts[-1]
    assert "Gemini" in text and "Media Watermark" in text and "فاز ۲" not in text
