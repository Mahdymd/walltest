"""Telegram admin panel: previews, buttons, reply shortcuts, conversations, commands, alerts.

Everything here talks to the ADMIN only. Channel sends live in publisher.py.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
import warnings
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)

from . import geministate, gemini_post, imagegen, pipeline, publisher, topics
from .config import ARTIFACTS_DIR, DATA_DIR, LOG_PATH, LOGO_PATH, load_config
from .context import AppContext, reload_rules
from .images import ImageError, process_image
from .imagegen import ImageGenError
from .logsetup import redact
from .policy import Report
from .sheets import clean

log = logging.getLogger(__name__)
TEXT, PHOTO, REJECT_TEXT, PUB_EDIT = range(4)
REASONS = ["موضوع نامناسب", "تکراری", "اطلاعات نادرست", "کیفیت تصویر", "سایر"]
STAGES = ["📥 خواندن", "🛡 سیاست‌ها", "🖼 تصویر", "📨 پیش‌نمایش"]
HELP_TEXT = (
    "راهنمای ربات وال\n\n"
    "/status — وضعیت کلی\n"
    "/sync — خواندن فوری شیت\n"
    "/queue — پیش‌نویس‌های منتظر تأیید\n"
    "/draft W-0012 — ارسال دوباره‌ی پیش‌نمایش\n"
    "/reload — بارگذاری دوباره‌ی تنظیمات\n"
    "/log 30 — آخرین خطوط لاگ\n"
    "/new موضوع — ساخت پست از یک موضوع (متن با Gemini، عکس از منابع مجاز یا Gemini)\n"
    "/topic موضوع — اضافه کردن موضوع به صف خودکار (هر ساعت یک پست)\n"
    "/topics — نمایش صف موضوع‌ها\n"
    "/testimage — ساخت یک تصویر تست با Gemini (منتشر نمی‌شود)\n"
    "/pause — توقف ساخت خودکار تصویر با Gemini\n"
    "/resume — ادامه‌ی ساخت خودکار تصویر\n"
    "/cancel — لغو کار در حال انجام\n\n"
    "روی پیش‌نمایش، Reply با متن جدید = ویرایش متن؛ Reply با عکس = آپلود عکس."
)


def preview_keyboard(pid: str, ver: int) -> InlineKeyboardMarkup:
    def b(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"act:{pid}:{ver}:{action}")
    return InlineKeyboardMarkup([
        [b("✅ انتشار", "pub"), b("🔕 انتشار بی‌صدا", "spub")],
        [b("✏️ ویرایش", "edit"), b("📤 آپلود عکس", "upl")],
        [b("❌ رد", "rej"), b("🗑 حذف", "del")],
    ])


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> AppContext:
    return context.application.bot_data["ctx"]


def _notifier(context: ContextTypes.DEFAULT_TYPE) -> "TgNotifier":
    return context.application.bot_data["notifier"]


def admin_only(fn):  # noqa: ANN001, ANN201
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN202
        user = update.effective_user
        ctx = _ctx(context)
        if user is None or user.id != ctx.env.admin_user_id:
            log.warning("ignored update from non-admin user id=%s", user.id if user else None)
            if update.callback_query:
                await update.callback_query.answer()
            return ConversationHandler.END
        return await fn(update, context)
    return wrapper


class TgNotifier:
    """Implements pipeline.Notifier by sending messages to the admin."""

    def __init__(self, bot, ctx: AppContext) -> None:  # noqa: ANN001
        self.bot = bot
        self.ctx = ctx
        self._progress: dict[str, tuple[int, float]] = {}
        self._alert_seen: dict[str, float] = {}

    @property
    def admin(self) -> int:
        return self.ctx.env.admin_user_id

    async def say(self, text: str, **kw) -> object:  # noqa: ANN003
        return await self.bot.send_message(chat_id=self.admin, text=text, **kw)

    async def progress(self, post_id: str, stage: int, note: str = "") -> None:
        labels = []
        for i, lab in enumerate(STAGES, start=1):
            labels.append(f"{'✅' if i < stage else ('⏳' if i == stage else '·')} {lab}")
        text = f"{post_id}: " + " ← ".join(labels) + (f"\n{note}" if note else "")
        state = self._progress.get(post_id)
        try:
            if state is None:
                msg = await self.say(text)
                self._progress[post_id] = (msg.message_id, time.time())
            else:
                mid, last = state
                if time.time() - last >= 3:
                    await self.bot.edit_message_text(chat_id=self.admin, message_id=mid, text=text)
                    self._progress[post_id] = (mid, time.time())
        except TelegramError:
            pass

    async def _retire(self, post_id: str, keep_version: int | None) -> int | None:
        first = None
        for pv in self.ctx.store.previews_for_post(post_id):
            if keep_version is not None and pv["version"] == keep_version:
                continue
            first = first or pv["message_id"]
            try:
                await self.bot.edit_message_reply_markup(chat_id=pv["chat_id"], message_id=pv["message_id"],
                                                         reply_markup=None)
            except TelegramError:
                pass
        return first

    async def draft_ready(self, post_id: str) -> None:
        store = self.ctx.store
        post = store.get_post(post_id)
        if post is None:
            return
        ver = post["version"]
        await self._retire(post_id, keep_version=ver)
        kb = preview_keyboard(post_id, ver)
        caption = post["caption_html"]
        if post["image_path"] and Path(post["image_path"]).exists():
            with open(post["image_path"], "rb") as fh:
                msg = await self.bot.send_photo(chat_id=self.admin, photo=fh, caption=caption,
                                                parse_mode=ParseMode.HTML, reply_markup=kb)
            store.set_fields(post_id, image_file_id=msg.photo[-1].file_id)
        else:
            msg = await self.bot.send_message(chat_id=self.admin, text=caption, parse_mode=ParseMode.HTML,
                                              disable_web_page_preview=True, reply_markup=kb)
        store.preview_add(msg.message_id, self.admin, post_id, ver)
        report = Report.from_json(post["report_json"])
        rep_msg = await self.bot.send_message(chat_id=self.admin, text=report.render(ver),
                                              reply_to_message_id=msg.message_id)
        store.preview_add(rep_msg.message_id, self.admin, post_id, ver)
        state = self._progress.pop(post_id, None)
        if state:
            try:
                await self.bot.edit_message_text(chat_id=self.admin, message_id=state[0],
                                                 text=f"{post_id}: ✅ پیش‌نمایش نسخه {ver} آماده شد")
            except TelegramError:
                pass

    async def blocked(self, post_id: str, report: Report) -> None:
        self._progress.pop(post_id, None)
        await self.say(f"⛔ {post_id} نیاز به اصلاح در شیت دارد. دکمه‌ی انتشار نمایش داده نمی‌شود.\n\n"
                       + report.render())

    async def stale(self, post_id: str, version: int, reason: str) -> None:
        first = await self._retire(post_id, keep_version=None)
        text = (f"{post_id} — نسخه {version}: منسوخ — {reason}.\n"
                "برای پیش‌نویس جدید، سطر را در شیت روی Pending بگذارید.")
        kw = {"reply_to_message_id": first} if first else {}
        await self.say(text, **kw)

    async def alert(self, text: str) -> None:
        key = text[:40]
        cooldown = self.ctx.cfg.admin.alert_cooldown_minutes * 60
        if time.time() - self._alert_seen.get(key, 0) < cooldown:
            return
        self._alert_seen[key] = time.time()
        try:
            await self.say(text)
        except TelegramError as exc:
            log.warning("alert send failed: %s", str(exc)[:120])


# ---------------------------------------------------------------- shared actions

async def _answer(q, text: str = "", alert: bool = False) -> None:  # noqa: ANN001
    try:
        await q.answer(text=text or None, show_alert=alert)
    except TelegramError:
        pass


async def do_reject(context: ContextTypes.DEFAULT_TYPE, pid: str, ver: int, reason: str, actor: str) -> str:
    ctx = _ctx(context)
    post = ctx.store.get_post(pid)
    if post is None or post["version"] != ver or post["status"] != "Drafted":
        return "این پیش‌نمایش دیگر فعال نیست."
    ctx.store.transition(pid, "Drafted", "Rejected", version=ver)
    ctx.store.set_fields(pid, last_error="")
    ctx.store.outbox_add(pid, "Status", "Rejected")
    ctx.store.outbox_add(pid, "RejectReason", reason)
    ctx.store.outbox_add(pid, "UpdatedAt", pipeline.tehran_now())
    ctx.store.audit(pid, actor, "reject", ver, reason)
    await _notifier(context)._retire(pid, keep_version=None)
    await pipeline.flush_now(ctx)
    return f"❌ {pid} رد شد. دلیل: {reason}"


async def do_delete(context: ContextTypes.DEFAULT_TYPE, pid: str, ver: int, actor: str) -> str:
    ctx = _ctx(context)
    post = ctx.store.get_post(pid)
    if post is None or post["status"] != "Drafted" or post["version"] != ver:
        return "این پیش‌نمایش دیگر فعال نیست."
    ctx.store.transition(pid, "Drafted", "Deleted", version=ver)
    for pv in ctx.store.previews_for_post(pid):
        try:
            await context.bot.delete_message(chat_id=pv["chat_id"], message_id=pv["message_id"])
        except TelegramError:
            pass
    ctx.store.preview_forget_post(pid)
    ctx.store.outbox_add(pid, "Status", "Deleted")
    ctx.store.outbox_add(pid, "AdminNote", "حذف توسط ادمین")
    ctx.store.outbox_add(pid, "UpdatedAt", pipeline.tehran_now())
    ctx.store.audit(pid, actor, "delete_draft", ver)
    await pipeline.flush_now(ctx)
    return f"🗑 {pid} حذف شد."


async def do_publish(context: ContextTypes.DEFAULT_TYPE, chat_id: int, pid: str, ver: int,
                     silent: bool, actor: str) -> str:
    ctx = _ctx(context)
    notifier = _notifier(context)
    out = await publisher.publish(context.bot, ctx, pid, ver, silent, actor)
    if out.kind == "ok":
        await notifier._retire(pid, keep_version=None)
        link = out.post_url or f"پیام {out.message_id} در کانال"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ اصلاح کپشن", callback_data=f"pub:{pid}:edit"),
            InlineKeyboardButton("🗑 حذف از کانال", callback_data=f"pub:{pid}:del"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {pid} منتشر شد: {link}",
                                       reply_markup=kb, disable_web_page_preview=False)
        return ""
    if out.kind == "unknown":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله، منتشر شد (ثبت کن)", callback_data=f"rec:{pid}:yes"),
            InlineKeyboardButton("🔁 نه، دوباره بفرست", callback_data=f"rec:{pid}:no"),
        ]])
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"❓ {pid}: {out.message}\nآیا {pid} در کانال منتشر شده است؟",
                                       reply_markup=kb)
        return ""
    if out.kind == "stale":
        await notifier.stale(pid, ver, "محتوا در شیت تغییر کرد")
    return out.message


# ---------------------------------------------------------------- callbacks

@admin_only
async def on_act(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) < 4:
        await _answer(q)
        return ConversationHandler.END
    _, pid, ver_s, action, *extra = parts
    try:
        ver = int(ver_s)
    except ValueError:
        await _answer(q)
        return ConversationHandler.END
    ctx = _ctx(context)
    actor = f"admin:{update.effective_user.id}"
    post = ctx.store.get_post(pid)
    if post is None:
        await _answer(q, "پست پیدا نشد", alert=True)
        return ConversationHandler.END
    if post["version"] != ver and action not in ("show",):
        await _answer(q, "این پیش‌نمایش قدیمی است", alert=True)
        return ConversationHandler.END
    if post["stale"] and action in ("pub", "spub", "go", "gos", "upl", "etxt", "rr"):
        await _answer(q, "منسوخ — محتوا در شیت تغییر کرد", alert=True)
        return ConversationHandler.END
    chat_id = q.message.chat_id if q.message else ctx.env.admin_user_id

    if action in ("pub", "spub"):
        report = Report.from_json(post["report_json"])
        if report.verdict == "BLOCK":
            await _answer(q, "⛔ این پست قابل انتشار نیست", alert=True)
            return ConversationHandler.END
        silent = action == "spub"
        if report.verdict == "REVIEW":
            await _answer(q)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ بله، منتشر کن", callback_data=f"act:{pid}:{ver}:{'gos' if silent else 'go'}"),
                InlineKeyboardButton("↩️ انصراف", callback_data=f"act:{pid}:{ver}:cancel"),
            ]])
            await context.bot.send_message(chat_id=chat_id, reply_markup=kb,
                                           text=f"⚠️ {pid} موارد ⚠️ دارد. آیا همه را بررسی کرده‌اید؟")
            return ConversationHandler.END
        await _answer(q, "در حال انتشار…")
        msg = await do_publish(context, chat_id, pid, ver, silent, actor)
        if msg:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        return ConversationHandler.END

    if action in ("go", "gos"):
        await _answer(q, "در حال انتشار…")
        msg = await do_publish(context, chat_id, pid, ver, action == "gos", actor)
        if msg:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        return ConversationHandler.END

    if action == "cancel":
        await _answer(q)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return ConversationHandler.END

    if action == "edit":
        await _answer(q)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 متن جدید را همین‌جا می‌فرستم", callback_data=f"act:{pid}:{ver}:etxt"),
            InlineKeyboardButton("📄 در شیت اصلاح می‌کنم", callback_data=f"act:{pid}:{ver}:esheet"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=f"{pid}: چه کاری انجام دهم؟", reply_markup=kb)
        return ConversationHandler.END

    if action == "etxt":
        await _answer(q)
        context.user_data["pending"] = {"kind": "etxt", "pid": pid, "ver": ver}
        await context.bot.send_message(chat_id=chat_id,
                                       text="📝 متن جدید پست را بفرستید (با قالب‌بندی تلگرام). برای لغو: /cancel")
        return TEXT

    if action == "esheet":
        await _answer(q)
        if ctx.store.transition(pid, "Drafted", "NeedsFix", version=ver):
            ctx.store.outbox_add(pid, "Status", "NeedsFix")
            ctx.store.outbox_add(pid, "AdminNote", "در شیت اصلاح کنید، سپس Status را Pending بگذارید")
            ctx.store.audit(pid, actor, "edit_in_sheet", ver)
            await _notifier(context)._retire(pid, keep_version=None)
            await pipeline.flush_now(ctx)
            await context.bot.send_message(chat_id=chat_id, text=(
                f"📄 {pid} به NeedsFix رفت. متن را در شیت اصلاح کنید و Status را روی Pending بگذارید."))
        return ConversationHandler.END

    if action == "upl":
        await _answer(q)
        context.user_data["pending"] = {"kind": "upl", "pid": pid, "ver": ver}
        await context.bot.send_message(chat_id=chat_id, text="📤 عکس را بفرستید (به‌صورت عکس یا فایل تصویری).")
        return PHOTO

    if action == "rej":
        await _answer(q)
        rows = [[InlineKeyboardButton(r, callback_data=f"act:{pid}:{ver}:rr:{i}")] for i, r in enumerate(REASONS)]
        await context.bot.send_message(chat_id=chat_id, text=f"دلیل رد {pid}؟",
                                       reply_markup=InlineKeyboardMarkup(rows))
        return ConversationHandler.END

    if action == "rr":
        idx = int(extra[0]) if extra and extra[0].isdigit() else 0
        if idx >= len(REASONS) - 1:
            await _answer(q)
            context.user_data["pending"] = {"kind": "reject", "pid": pid, "ver": ver}
            await context.bot.send_message(chat_id=chat_id, text="متن دلیل رد را بفرستید. برای لغو: /cancel")
            return REJECT_TEXT
        await _answer(q)
        msg = await do_reject(context, pid, ver, REASONS[idx], actor)
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return ConversationHandler.END

    if action == "del":
        await _answer(q)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 بله، حذف کن", callback_data=f"act:{pid}:{ver}:delok"),
            InlineKeyboardButton("↩️ انصراف", callback_data=f"act:{pid}:{ver}:cancel"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=f"{pid} حذف شود؟", reply_markup=kb)
        return ConversationHandler.END

    if action == "delok":
        await _answer(q)
        msg = await do_delete(context, pid, ver, actor)
        await context.bot.send_message(chat_id=chat_id, text=msg)
        return ConversationHandler.END

    if action == "show":
        await _answer(q)
        if post["status"] == "Drafted":
            await _notifier(context).draft_ready(pid)
        return ConversationHandler.END

    await _answer(q)
    return ConversationHandler.END


@admin_only
async def on_pub_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    q = update.callback_query
    _, pid, action = (q.data or "").split(":")[:3]
    ctx = _ctx(context)
    post = ctx.store.get_post(pid)
    if post is None or post["status"] != "Published":
        await _answer(q, "این پست منتشرشده نیست.", alert=True)
        return ConversationHandler.END
    chat_id = q.message.chat_id if q.message else ctx.env.admin_user_id
    if action == "edit":
        await _answer(q)
        context.user_data["pending"] = {"kind": "pubedit", "pid": pid, "ver": post["version"]}
        await context.bot.send_message(chat_id=chat_id, text="✏️ کپشن جدید را بفرستید. برای لغو: /cancel")
        return PUB_EDIT
    if action == "del":
        await _answer(q)
        window = ctx.cfg.admin.publish_delete_window_hours * 3600
        if not post["published_at"] or time.time() - post["published_at"] > window:
            await context.bot.send_message(chat_id=chat_id, text=(
                f"⏳ حذف از کانال فقط تا {ctx.cfg.admin.publish_delete_window_hours} ساعت بعد از انتشار ممکن است. "
                "پیام را دستی در کانال حذف کنید."))
            return ConversationHandler.END
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 بله، از کانال حذف کن", callback_data=f"pub:{pid}:delok"),
            InlineKeyboardButton("↩️ انصراف", callback_data=f"pub:{pid}:cancel"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=f"{pid} از کانال حذف شود؟", reply_markup=kb)
        return ConversationHandler.END
    if action == "delok":
        await _answer(q)
        try:
            await publisher.delete_channel_message(context.bot, ctx.env.channel_id, post["channel_message_id"])
        except TelegramError as exc:
            await context.bot.send_message(chat_id=chat_id, text=publisher.persian_error(exc))
            return ConversationHandler.END
        ctx.store.transition(pid, "Published", "Deleted")
        ctx.store.outbox_add(pid, "Status", "Deleted")
        ctx.store.outbox_add(pid, "AdminNote", "حذف از کانال توسط ادمین")
        ctx.store.audit(pid, f"admin:{update.effective_user.id}", "delete_channel", post["version"])
        await pipeline.flush_now(ctx)
        await context.bot.send_message(chat_id=chat_id, text=f"🗑 {pid} از کانال حذف شد.")
        return ConversationHandler.END
    await _answer(q)
    return ConversationHandler.END


@admin_only
async def on_rec(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    q = update.callback_query
    _, pid, answer = (q.data or "").split(":")[:3]
    ctx = _ctx(context)
    post = ctx.store.get_post(pid)
    await _answer(q)
    if post is None or post["status"] != "Publishing":
        await context.bot.send_message(chat_id=ctx.env.admin_user_id, text=f"{pid} دیگر در وضعیت انتشار نیست.")
        return ConversationHandler.END
    out = await publisher.resolve_unknown(context.bot, ctx, pid, post["version"], answer == "yes",
                                          bool(post["silent"]), f"admin:{update.effective_user.id}")
    if out.kind == "ok":
        await _notifier(context)._retire(pid, keep_version=None)
        await context.bot.send_message(chat_id=ctx.env.admin_user_id,
                                       text=f"✅ {pid} ثبت شد. {out.post_url or ''}".strip())
    else:
        await context.bot.send_message(chat_id=ctx.env.admin_user_id, text=out.message)
    return ConversationHandler.END


@admin_only
async def on_conflict(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    q = update.callback_query
    _, pid, ver_s, choice = (q.data or "").split(":")[:4]
    ctx = _ctx(context)
    await _answer(q)
    post = ctx.store.get_post(pid)
    if post is None or post["version"] != int(ver_s):
        await context.bot.send_message(chat_id=ctx.env.admin_user_id, text="این پیش‌نمایش قدیمی است.")
        return ConversationHandler.END
    actor = f"admin:{update.effective_user.id}"
    if choice == "tg":
        text_html = ctx.store.kv_get(f"conflict:{pid}")
        if not text_html:
            await context.bot.send_message(chat_id=ctx.env.admin_user_id, text="متن تلگرام پیدا نشد؛ دوباره ویرایش کنید.")
            return ConversationHandler.END
        out = await pipeline.write_fulltext_and_redraft(ctx, _notifier(context), pid, text_html, actor)
    else:
        out = await pipeline.redraft(ctx, _notifier(context), pid, actor=actor)
    ctx.store.kv_set(f"conflict:{pid}", "")
    if out.status != "Drafted":
        await context.bot.send_message(chat_id=ctx.env.admin_user_id, text=f"{pid}: {out.message}")
    return ConversationHandler.END


# ---------------------------------------------------------------- text / photo input

async def apply_text(update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str, ver: int) -> None:
    ctx = _ctx(context)
    msg = update.effective_message
    text_html = (msg.text_html or "").strip()
    if not text_html:
        await msg.reply_text("متن خالی است. دوباره بفرستید.")
        return
    post = ctx.store.get_post(pid)
    if post is None or post["version"] != ver or post["status"] != "Drafted":
        await msg.reply_text("این پیش‌نمایش قدیمی است یا دیگر فعال نیست.")
        return
    if post["stale"]:
        await msg.reply_text("این پیش‌نمایش منسوخ شده است.")
        return
    actor = f"admin:{update.effective_user.id}"
    await msg.reply_text(f"⏳ {pid}: در حال ثبت متن و ساخت پیش‌نویس جدید…")
    out = await pipeline.edit_text(ctx, _notifier(context), pid, text_html, actor)
    if out == "conflict":
        ctx.store.kv_set(f"conflict:{pid}", text_html)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📱 نسخه‌ی تلگرام", callback_data=f"cf:{pid}:{ver}:tg"),
            InlineKeyboardButton("📄 نسخه‌ی شیت", callback_data=f"cf:{pid}:{ver}:sh"),
        ]])
        await msg.reply_text(f"⚠️ {pid}: متن در شیت هم تغییر کرده است. کدام نسخه معتبر است؟", reply_markup=kb)
        return
    if isinstance(out, pipeline.Outcome) and out.status != "Drafted":
        await msg.reply_text(f"{pid}: {out.message}")


async def apply_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str, ver: int) -> None:
    ctx = _ctx(context)
    msg = update.effective_message
    post = ctx.store.get_post(pid)
    if post is None or post["version"] != ver or post["status"] != "Drafted":
        await msg.reply_text("این پیش‌نمایش قدیمی است یا دیگر فعال نیست.")
        return
    file_obj = None
    if msg.photo:
        file_obj = await msg.photo[-1].get_file()
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_obj = await msg.document.get_file()
    if file_obj is None:
        await msg.reply_text("لطفاً یک عکس بفرستید.")
        return
    tmp = DATA_DIR / "cache" / f"upload_{pid}_{int(time.time())}.bin"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    await file_obj.download_to_drive(custom_path=str(tmp))
    await msg.reply_text(f"⏳ {pid}: در حال پردازش عکس (کراپ ۱۶:۹ و لوگو)…")
    try:
        out = await pipeline.upload_image(ctx, _notifier(context), pid, tmp, f"admin:{update.effective_user.id}")
    except ImageError as exc:
        await msg.reply_text(str(exc))
        return
    finally:
        tmp.unlink(missing_ok=True)
    if out.status != "Drafted":
        await msg.reply_text(f"{pid}: {out.message}")


@admin_only
async def on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    pending = context.user_data.pop("pending", None) or {}
    kind, pid, ver = pending.get("kind"), pending.get("pid"), pending.get("ver")
    if kind == "etxt":
        await apply_text(update, context, pid, ver)
    elif kind == "reject":
        reason = clean(update.effective_message.text or "")[:200] or "سایر"
        msg = await do_reject(context, pid, ver, reason, f"admin:{update.effective_user.id}")
        await update.effective_message.reply_text(msg)
    elif kind == "pubedit":
        await _apply_pub_edit(update, context, pid)
    return ConversationHandler.END


@admin_only
async def on_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    pending = context.user_data.pop("pending", None) or {}
    if pending.get("kind") == "upl":
        await apply_photo(update, context, pending["pid"], pending["ver"])
    return ConversationHandler.END


@admin_only
async def on_reject_text(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    return await on_text_input(update, context)


async def _apply_pub_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str) -> None:
    ctx = _ctx(context)
    post = ctx.store.get_post(pid)
    if post is None or post["status"] != "Published":
        await update.effective_message.reply_text("این پست دیگر منتشرشده نیست.")
        return
    text_html = (update.effective_message.text_html or "").strip()
    try:
        await publisher.edit_channel_caption(context.bot, ctx.env.channel_id, post["channel_message_id"],
                                             text_html, has_photo=bool(post["image_file_id"]))
    except TelegramError as exc:
        await update.effective_message.reply_text(publisher.persian_error(exc))
        return
    ctx.store.set_fields(pid, caption_html=text_html)
    ctx.store.audit(pid, f"admin:{update.effective_user.id}", "edit_published_caption", post["version"])
    await update.effective_message.reply_text(f"✏️ کپشن {pid} در کانال اصلاح شد.")


@admin_only
async def on_reply_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    msg = update.effective_message
    target = msg.reply_to_message
    if target is None:
        return ConversationHandler.END
    pv = ctx.store.preview_lookup(target.message_id, ctx.env.admin_user_id)
    if pv is None:
        return ConversationHandler.END
    pid, ver = pv["post_id"], pv["version"]
    if msg.photo or msg.document:
        await apply_photo(update, context, pid, ver)
    elif msg.text:
        await apply_text(update, context, pid, ver)
    return ConversationHandler.END


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    context.user_data.pop("pending", None)
    if update.effective_message:
        await update.effective_message.reply_text("لغو شد.")
    return ConversationHandler.END


# ---------------------------------------------------------------- commands

def gemini_status_line(ctx: AppContext) -> str:
    from . import gemini
    try:
        gemini.require_verified(gemini.load_selectors())
        selectors = "سلکتورها تأیید شده"
    except gemini.GeminiError:
        selectors = "سلکتورها تأیید نشده (--inspect)"
    state = "⏸ متوقف (/resume)" if geministate.is_paused(ctx.store) else "▶️ فعال"
    used = geministate.used_today(ctx.store)
    return (f"🌐 Gemini: {state} | {selectors} | امروز {used}/{ctx.cfg.gemini.max_images_per_day} تصویر")


@admin_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    geministate.set_paused(_ctx(context).store, True)
    await update.effective_message.reply_text(
        "⏸ ساخت تصویر با Gemini متوقف شد.\n"
        "ردیف‌های ImageMode=gemini در شیت Pending می‌مانند. انتشار و بقیه‌ی کارها ادامه دارند.\n"
        "برای ادامه: /resume")
    return ConversationHandler.END


@admin_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    geministate.set_paused(_ctx(context).store, False)
    await update.effective_message.reply_text(
        "▶️ ساخت تصویر با Gemini دوباره فعال شد. ردیف‌های Pending در چرخه‌ی بعدی یا با /sync پردازش می‌شوند.")
    return ConversationHandler.END


@admin_only
async def cmd_testimage(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    msg = update.effective_message
    try:
        new_chat = imagegen.prepare_attempt(ctx)
    except ImageGenError as exc:
        await msg.reply_text(f"❌ {exc.message}")
        return ConversationHandler.END
    await msg.reply_text("🧪 در حال ساخت تصویر تست با Gemini… ممکن است چند دقیقه طول بکشد.")
    test_dir = ARTIFACTS_DIR / "testimage"
    try:
        async with pipeline.gemini_lock(ctx):
            raw = await asyncio.to_thread(imagegen.generate_blocking, imagegen.TEST_PROMPT, test_dir,
                                          ctx.cfg.gemini.timeout_seconds, new_chat)
        out = test_dir / f"test_{int(time.time())}.jpg"
        await asyncio.to_thread(process_image, raw, out, LOGO_PATH, ctx.cfg.images)
    except ImageGenError as exc:
        await msg.reply_text(f"❌ {exc.message}")
        return ConversationHandler.END
    except ImageError as exc:
        await msg.reply_text(f"❌ تصویر تست قابل استفاده نیست: {exc}")
        return ConversationHandler.END
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("بله، نشان Gemini دارد", callback_data="wm:yes"),
        InlineKeyboardButton("نه، ندارد", callback_data="wm:no"),
    ]])
    with out.open("rb") as fh:
        await msg.reply_photo(
            photo=fh, reply_markup=kb,
            caption=("🧪 تصویر تست (منتشر نمی‌شود).\n"
                     "🖼 آیا روی این تصویر نشان واترمارک خود Gemini (ستاره‌ی کوچک) دیده می‌شود؟ "
                     "لوگوی وال را در نظر نگیرید."))
    return ConversationHandler.END


_BACKGROUND: set[asyncio.Task] = set()  # keeps long jobs referenced until they finish


async def _new_post_job(ctx: AppContext, notifier, brief: str) -> None:  # noqa: ANN001
    try:
        out = await pipeline.new_post_from_brief(ctx, notifier, brief)
    except Exception as exc:  # noqa: BLE001
        log.exception("new post job failed")
        await notifier.alert(f"❌ ساخت پست ناموفق بود: {str(exc)[:200]}")
        return
    if not out.post_id and out.status == "Failed":
        await notifier.alert(f"❌ {out.message}")


@admin_only
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    msg = update.effective_message
    brief = " ".join(context.args or []).strip()
    if len(brief) < 5:
        await msg.reply_text("مثال:\n/new مزایای ذخیره‌ی انرژی در خانه‌های کوچک\n\nموضوع را کوتاه و واضح بنویسید.")
        return ConversationHandler.END
    try:
        gemini_post.check_ready(ctx)
    except gemini_post.PostError as exc:
        await msg.reply_text(f"❌ {exc}")
        return ConversationHandler.END
    await msg.reply_text(
        "✍️ متن پست با Gemini نوشته می‌شود و بعد دنبال عکس مجاز می‌گردم "
        "(اگر نبود، Gemini تصویر می‌سازد). چند دقیقه طول می‌کشد؛ پیش‌نمایش جداگانه می‌آید.")
    task = asyncio.create_task(_new_post_job(ctx, _notifier(context), brief))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return ConversationHandler.END


@admin_only
async def on_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    q = update.callback_query
    ctx = _ctx(context)
    geministate.set_watermark(ctx.store, q.data == "wm:yes")
    await q.answer("ثبت شد")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await q.message.reply_text(f"🖼 Media Watermark ثبت شد: {geministate.watermark_label(ctx.store)}")


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    await update.effective_message.reply_text("سلام! ربات وال آماده است.\n\n" + HELP_TEXT)
    return ConversationHandler.END


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    counts = ctx.store.status_counts()
    last = ctx.store.kv_get("last_sync", "")
    last_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last))) if last else "هنوز نه"
    size_mb = sum(p.stat().st_size for p in DATA_DIR.rglob("*") if p.is_file()) / (1024 * 1024) if DATA_DIR.exists() else 0
    lines = [
        f"📊 وضعیت وال — نسخه {ctx.cfg.version}",
        f"⏱ روشن از: {time.strftime('%H:%M', time.localtime(ctx.started_at))}",
        "🤖 تلگرام: ✅",
        f"📄 آخرین خواندن شیت: {last_txt}" + ("" if not ctx.sheet_fail_since else " ❌ (خطا)"),
        f"📤 در صف نوشتن در شیت: {ctx.store.outbox_count()}",
        gemini_status_line(ctx),
        f"🖼 Media Watermark: {geministate.watermark_label(ctx.store)}",
        "📦 سطرها: " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "خالی"),
        f"💾 حجم data: {size_mb:.1f} MB",
    ]
    await update.effective_message.reply_text("\n".join(lines))
    return ConversationHandler.END


@admin_only
async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    await update.effective_message.reply_text("🔄 در حال خواندن شیت…")
    async with ctx.lock:
        stats = await pipeline.sync_once(ctx, _notifier(context))
    await update.effective_message.reply_text(
        f"✅ خواندن انجام شد. Pending: {stats['pending']} | پردازش‌شده: {stats['processed']} | منسوخ‌شده: {stats['stale']}")
    return ConversationHandler.END


@admin_only
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    drafts = [p for p in ctx.store.posts_with_status("Drafted")][:10]
    if not drafts:
        await update.effective_message.reply_text("صف تأیید خالی است.")
        return ConversationHandler.END
    for p in drafts:
        rep = Report.from_json(p["report_json"])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("نمایش دوباره", callback_data=f"act:{p['id']}:{p['version']}:show")]])
        await update.effective_message.reply_text(f"{p['id']} — نسخه {p['version']} — {rep.summary}", reply_markup=kb)
    return ConversationHandler.END


@admin_only
async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("مثال: /draft W-0012")
        return ConversationHandler.END
    pid = args[0].upper()
    post = ctx.store.get_post(pid)
    if post is None or post["status"] != "Drafted":
        await update.effective_message.reply_text(f"{pid} پیش‌نویس آماده ندارد.")
        return ConversationHandler.END
    await _notifier(context).draft_ready(pid)
    return ConversationHandler.END


@admin_only
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    ctx = _ctx(context)
    try:
        ctx.cfg = load_config()
        notes = await asyncio.to_thread(reload_rules, ctx)
        await update.effective_message.reply_text("♻️ تنظیمات دوباره بارگذاری شد.\n" + "\n".join(notes))
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"بارگذاری ناموفق بود: {str(exc)[:200]}")
    return ConversationHandler.END


@admin_only
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    n = 30
    if context.args and context.args[0].isdigit():
        n = max(1, min(200, int(context.args[0])))
    if not LOG_PATH.exists():
        await update.effective_message.reply_text("فایل لاگ هنوز ساخته نشده است.")
        return ConversationHandler.END
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    text = redact("\n".join(lines))[-3500:]
    await update.effective_message.reply_text(text or "لاگ خالی است.")
    return ConversationHandler.END


# ---------------------------------------------------------------- startup / errors / jobs

async def _post_init(app: Application) -> None:
    ctx = app.bot_data["ctx"]
    ctx.lock = asyncio.Lock()
    ctx.started_at = time.time()
    store = ctx.store
    for p in store.posts_with_status("Processing"):
        store.transition(p["id"], "Processing", "Pending")
        store.outbox_add(p["id"], "Status", "Pending")
        log.info("%s was Processing at startup; back to Pending", p["id"])
    for p in store.posts_with_status("Publishing"):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله، منتشر شد (ثبت کن)", callback_data=f"rec:{p['id']}:yes"),
            InlineKeyboardButton("🔁 نه، دوباره بفرست", callback_data=f"rec:{p['id']}:no"),
        ]])
        await app.bot.send_message(chat_id=ctx.env.admin_user_id, reply_markup=kb, text=(
            f"❓ {p['id']} هنگام قطع ربات در حال انتشار بود. آیا در کانال منتشر شده است؟"))
    await pipeline.flush_now(ctx)
    notes = await asyncio.to_thread(reload_rules, ctx)
    log.info("rules loaded: %s", "؛ ".join(notes))
    await app.bot.send_message(chat_id=ctx.env.admin_user_id, text="🟢 ربات وال روشن شد.\n/status را بزنید.")


async def _sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    if ctx.lock is None:
        return
    async with ctx.lock:
        try:
            await pipeline.sync_once(ctx, _notifier(context))
        except Exception as exc:  # noqa: BLE001
            log.exception("sync failed")
            await _notifier(context).alert(f"⚠️ خطای همگام‌سازی: {str(exc)[:200]}")


async def _topics_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    try:
        reason = await topics.run_next(ctx, _notifier(context))
        log.info("topics tick: %s", reason)
    except Exception as exc:  # noqa: BLE001
        log.exception("topics job failed")
        await _notifier(context).alert(f"⚠️ خطا در پردازش موضوع‌ها: {str(exc)[:200]}")


@admin_only
async def cmd_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    msg = update.effective_message
    text = " ".join(context.args or []).strip()
    try:
        count = topics.add_topic(text)
    except ValueError as exc:
        await msg.reply_text(f"❌ {exc}\n\nمثال:\n/topic مزایای ذخیره‌ی انرژی در خانه‌های کوچک")
        return
    await msg.reply_text(f"✅ به صف اضافه شد. موضوع‌های در انتظار: {count}\nهر ساعت یک موضوع پردازش می‌شود.")


@admin_only
async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):  # noqa: ANN201
    items = topics.queued()
    if not items:
        await update.effective_message.reply_text("صف موضوع‌ها خالی است. با /topic موضوع جدید اضافه کنید.")
        return
    listing = "\n".join(f"{i}. {t[:80]}" for i, t in enumerate(items[:10], 1))
    more = f"\n… و {len(items) - 10} مورد دیگر" if len(items) > 10 else ""
    await update.effective_message.reply_text(f"موضوع‌های در انتظار ({len(items)}):\n{listing}{more}")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        log.error("409 Conflict: another bot instance is polling")
        print("ربات جای دیگری در حال اجراست (خطای 409 از تلگرام). این پنجره را ببندید و دوباره اجرا کنید.")
        context.application.stop_running()
        return
    if isinstance(err, RetryAfter):
        log.warning("RetryAfter %s", err.retry_after)
        return
    if isinstance(err, BadRequest) and "not modified" in str(err).lower():
        return
    log.error("unhandled error", exc_info=err)
    ctx = context.application.bot_data.get("ctx")
    notifier = context.application.bot_data.get("notifier")
    if ctx and notifier:
        await notifier.alert(f"⚠️ خطای غیرمنتظره: {type(err).__name__}: {str(err)[:160]}")


def build_application(ctx: AppContext) -> Application:
    builder = Application.builder().token(ctx.env.telegram_token).post_init(_post_init)
    if ctx.env.proxy_url:
        builder = builder.proxy(ctx.env.proxy_url).get_updates_proxy(ctx.env.proxy_url)
    app = builder.build()
    app.bot_data["ctx"] = ctx
    app.bot_data["notifier"] = TgNotifier(app.bot, ctx)

    admin = filters.User(user_id=ctx.env.admin_user_id)
    # per_message=False is deliberate: states are tracked per chat, not per message.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*per_message.*")
        conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(on_act, pattern=r"^act:"),
                          CallbackQueryHandler(on_pub_cb, pattern=r"^pub:"),
                          CallbackQueryHandler(on_rec, pattern=r"^rec:"),
                          CallbackQueryHandler(on_conflict, pattern=r"^cf:")],
            states={
                TEXT: [MessageHandler(admin & filters.TEXT & ~filters.COMMAND, on_text_input),
                       CallbackQueryHandler(on_act, pattern=r"^act:")],
                PHOTO: [MessageHandler(admin & (filters.PHOTO | filters.Document.IMAGE), on_photo_input),
                        CallbackQueryHandler(on_act, pattern=r"^act:")],
                REJECT_TEXT: [MessageHandler(admin & filters.TEXT & ~filters.COMMAND, on_reject_text),
                              CallbackQueryHandler(on_act, pattern=r"^act:")],
                PUB_EDIT: [MessageHandler(admin & filters.TEXT & ~filters.COMMAND, on_text_input),
                           CallbackQueryHandler(on_pub_cb, pattern=r"^pub:")],
            },
            fallbacks=[CommandHandler("cancel", on_cancel)],
            conversation_timeout=ctx.cfg.admin.conversation_timeout_seconds,
            per_message=False,
        )
    app.add_handler(conv)
    app.add_handler(MessageHandler(admin & filters.REPLY & (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                                   & ~filters.COMMAND, on_reply_shortcut))
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("status", cmd_status), ("sync", cmd_sync),
                     ("queue", cmd_queue), ("draft", cmd_draft), ("reload", cmd_reload), ("log", cmd_log),
                     ("pause", cmd_pause), ("resume", cmd_resume), ("testimage", cmd_testimage),
                     ("new", cmd_new), ("topic", cmd_topic), ("topics", cmd_topics)]:
        app.add_handler(CommandHandler(name, fn, filters=admin))
    app.add_handler(CallbackQueryHandler(on_watermark, pattern=r"^wm:"))
    app.add_error_handler(_on_error)
    app.job_queue.run_repeating(_sync_job, interval=ctx.cfg.sheet.poll_seconds, first=5, name="sheet-sync")
    if ctx.cfg.topics.enabled:
        app.job_queue.run_repeating(_topics_job, interval=ctx.cfg.topics.interval_minutes * 60,
                                    first=ctx.cfg.topics.first_run_minutes * 60, name="topics")
    return app


