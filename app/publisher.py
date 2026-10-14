"""The ONLY module allowed to send anything to the channel (CHANNEL_ID).

Exactly-once: Drafted -> Publishing is an atomic compare-and-set in SQLite.
The preview's file_id is used, so the published image is byte-identical to the one the admin saw.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from .context import AppContext
from .pipeline import flush_now, tehran_now, TEHRAN
from .sheets import human_hash
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class PublishOutcome:
    kind: str  # ok | unknown | stale | already | failed | blocked
    message: str = ""
    message_id: int | None = None
    post_url: str = ""


def persian_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "chat not found" in text:
        return "کانال پیدا نشد. CHANNEL_ID را در فایل .env بررسی کنید."
    if "not enough rights" in text or "have no rights" in text or "not enough rights to send" in text:
        return "ربات دسترسی ارسال در کانال ندارد. ربات را ادمین کانال کنید و دسترسی ارسال بدهید."
    if isinstance(exc, Forbidden):
        return "ربات از کانال حذف شده یا دسترسی ندارد."
    if "can't parse entities" in text or "unsupported start tag" in text or "can't find end tag" in text:
        return "کپشن قالب HTML نامعتبر دارد. متن را اصلاح کنید."
    if "wrong file identifier" in text or "wrong remote file" in text:
        return "عکس پیش‌نمایش دیگر معتبر نیست. عکس را دوباره آپلود کنید."
    if "caption is too long" in text:
        return "کپشن از سقف تلگرام بلندتر است."
    return f"ارسال به کانال ناموفق بود: {str(exc)[:160]}"


def channel_post_url(username: str | None, message_id: int) -> str:
    return f"https://t.me/{username}/{message_id}" if username else ""


async def send_to_channel(bot: Bot, channel_id: str, post: dict, silent: bool) -> int:
    """Send the stored draft to the channel. Returns the channel message_id."""
    if post["image_file_id"]:
        msg = await bot.send_photo(chat_id=channel_id, photo=post["image_file_id"],
                                   caption=post["caption_html"], parse_mode=ParseMode.HTML,
                                   disable_notification=silent)
    else:
        msg = await bot.send_message(chat_id=channel_id, text=post["caption_html"],
                                     parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                     disable_notification=silent)
    return msg.message_id


async def edit_channel_caption(bot: Bot, channel_id: str, message_id: int, caption_html: str,
                               has_photo: bool) -> None:
    if has_photo:
        await bot.edit_message_caption(chat_id=channel_id, message_id=message_id,
                                       caption=caption_html, parse_mode=ParseMode.HTML)
    else:
        await bot.edit_message_text(chat_id=channel_id, message_id=message_id,
                                    text=caption_html, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def delete_channel_message(bot: Bot, channel_id: str, message_id: int) -> None:
    await bot.delete_message(chat_id=channel_id, message_id=message_id)


async def check_channel_rights(bot: Bot, channel_id: str) -> list[str]:
    """Problems (Persian) with the bot's rights in the channel. Empty list = all good."""
    problems: list[str] = []
    try:
        me = await bot.get_me()
        await bot.get_chat(channel_id)
        member = await bot.get_chat_member(channel_id, me.id)
    except TelegramError as exc:
        return [persian_error(exc)]
    if member.status not in ("administrator", "creator"):
        return ["ربات در کانال ادمین نیست. آن را ادمین کانال کنید."]
    for flag, label in (("can_post_messages", "ارسال پست"), ("can_edit_messages", "ویرایش پست"),
                        ("can_delete_messages", "حذف پست")):
        if not getattr(member, flag, False):
            problems.append(f"دسترسی «{label}» به ربات داده نشده است.")
    return problems


def _can_publish(post: dict, version: int, store: Store) -> str | None:
    if post["status"] != "Drafted":
        return f"وضعیت این پست «{post['status']}» است و قابل انتشار نیست."
    if post["version"] != version:
        return "این پیش‌نمایش قدیمی است."
    if post["stale"]:
        return "این پیش‌نمایش منسوخ شده است. پیش‌نمایش جدید را ببینید."
    return None


async def publish(bot: Bot, ctx: AppContext, pid: str, version: int, silent: bool,
                  actor: str) -> PublishOutcome:
    store = ctx.store
    post = store.get_post(pid)
    if post is None:
        return PublishOutcome("failed", "پست پیدا نشد.")
    problem = _can_publish(post, version, store)
    if problem:
        return PublishOutcome("stale" if "قدیمی" in problem or "منسوخ" in problem else "already", problem)
    if post["caption_html"] == "":
        return PublishOutcome("failed", "کپشن خالی است.")

    # sheet must still match the draft (human did not change it, status still Drafted)
    if store.outbox_for(pid):
        return PublishOutcome("stale", "هنوز تغییرات این پست در شیت نوشته نشده است. چند ثانیه صبر کنید.")
    snap = await asyncio.to_thread(ctx.sheet.read_content)
    row = snap.find(pid)
    if row is None or row.status != "Drafted" or human_hash(row.values) != post["human_hash"]:
        store.set_fields(pid, stale=1)
        store.audit(pid, actor, "publish_refused_changed", version, "محتوای شیت تغییر کرده است")
        return PublishOutcome("stale", "محتوای این پست در شیت تغییر کرده است. پیش‌نمایش منسوخ شد.")

    if not store.transition(pid, "Drafted", "Publishing", version=version):
        return PublishOutcome("already", "این پست هم‌اکنون منتشر شده یا در حال انتشار است.")
    store.set_fields(pid, silent=int(silent))
    store.audit(pid, actor, "publish_start", version, "بی‌صدا" if silent else "")

    message_id: int | None = None
    channel = ctx.env.channel_id
    for attempt in range(2):
        try:
            message_id = await send_to_channel(bot, channel, post, silent)
            break
        except RetryAfter as exc:
            wait = float(exc.retry_after) + 1
            log.info("RetryAfter %.0fs while publishing %s", wait, pid)
            await asyncio.sleep(min(wait, 60))
            continue
        except (TimedOut, NetworkError) as exc:
            # the message may or may not have reached the channel: never retry blindly
            store.audit(pid, actor, "publish_unknown", version, str(exc)[:200])
            return PublishOutcome("unknown", "ارسال به کانال قطع شد؛ نمی‌دانیم پیام رسیده یا نه.")
        except (BadRequest, Forbidden, TelegramError) as exc:
            msg = persian_error(exc)
            store.transition(pid, "Publishing", "Drafted")
            store.set_fields(pid, last_error=msg[:200])
            store.audit(pid, actor, "publish_failed", version, msg)
            return PublishOutcome("failed", msg)
    if message_id is None:
        store.audit(pid, actor, "publish_unknown", version, "retry exhausted")
        return PublishOutcome("unknown", "ارسال به کانال انجام نشد؛ وضعیت را بررسی کنید.")
    return await _mark_published(bot, ctx, pid, version, message_id, actor)


async def _mark_published(bot: Bot, ctx: AppContext, pid: str, version: int, message_id: int,
                          actor: str) -> PublishOutcome:
    store = ctx.store
    username = None
    try:
        chat = await bot.get_chat(ctx.env.channel_id)
        username = chat.username
    except TelegramError:
        pass
    # message_id 0 means the send result was recovered by the admin ("yes"); no real URL is known.
    url = channel_post_url(username, message_id) if message_id else ""
    store.transition(pid, "Publishing", "Published", version=None)
    store.set_fields(pid, channel_message_id=message_id, post_url=url,
                     published_at=datetime.now(TEHRAN).timestamp(), last_error="")
    store.outbox_add(pid, "Status", "Published")
    store.outbox_add(pid, "PostURL", url or f"(کانال خصوصی) پیام {message_id}")
    store.outbox_add(pid, "PublishedAt", tehran_now())
    store.audit(pid, actor, "published", version, f"message_id={message_id}")
    await flush_now(ctx)
    return PublishOutcome("ok", "", message_id, url)


async def resolve_unknown(bot: Bot, ctx: AppContext, pid: str, version: int, did_publish: bool,
                          silent: bool, actor: str) -> PublishOutcome:
    """Admin answered the 'was it published?' question after a crash or network cut."""
    store = ctx.store
    post = store.get_post(pid)
    if post is None or post["status"] != "Publishing":
        return PublishOutcome("already", "این پست در وضعیت انتشار نیست.")
    if did_publish:
        store.audit(pid, actor, "recover_marked_published", version, "ادمین تأیید کرد")
        return await _mark_published(bot, ctx, pid, version, 0, actor)
    store.transition(pid, "Publishing", "Drafted")
    store.audit(pid, actor, "recover_resend", version, "ادمین گفت منتشر نشده")
    return await publish(bot, ctx, pid, version, silent, actor)
