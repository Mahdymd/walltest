"""Sheet row -> composed caption -> policy -> draft (or NeedsFix / Waiting / Failed).

The only place where sheet rows become posts. No AI, no publishing here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .composer import compose
from .config import IMAGES_DIR, LOGO_PATH
from .context import AppContext
from . import commons, geministate, gemini_post, imagegen
from .images import ImageError, hamming, process_image
from .imagegen import ImageGenError
from .policy import Report, PolicyContext, PolicyInput, SourceResult, evaluate
from .sheets import (COLUMNS, Row, Snapshot, clean, flush_outbox, fulltext_hash, human_hash)
from .sources import fetch_source
from .textutil import strip_html

log = logging.getLogger(__name__)
TEHRAN = ZoneInfo("Asia/Tehran")
ID_NUM = re.compile(r"^W-(\d+)$")
CONTENT_COLS = ("Title", "Body", "FullText")


def tehran_now() -> str:
    return datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M")


@dataclass
class EvalResult:
    status: str  # Drafted | NeedsFix | Waiting | Failed
    message: str = ""
    report: Report | None = None
    caption_html: str = ""
    caption_plain: str = ""
    image_path: str = ""
    image_mode: str = "none"
    notes: list[str] = field(default_factory=list)
    needs_gemini: bool = False  # Waiting only because the image must be generated


@dataclass
class Outcome:
    post_id: str
    status: str
    version: int = 0
    message: str = ""


class Notifier(Protocol):
    async def progress(self, post_id: str, stage: int, note: str = "") -> None: ...
    async def draft_ready(self, post_id: str) -> None: ...
    async def blocked(self, post_id: str, report: Report) -> None: ...
    async def stale(self, post_id: str, version: int, reason: str) -> None: ...
    async def alert(self, text: str) -> None: ...


class NullNotifier:
    async def progress(self, post_id: str, stage: int, note: str = "") -> None:
        return None

    async def draft_ready(self, post_id: str) -> None:
        return None

    async def blocked(self, post_id: str, report: Report) -> None:
        return None

    async def stale(self, post_id: str, version: int, reason: str) -> None:
        return None

    async def alert(self, text: str) -> None:
        return None


def evaluate_row(ctx: AppContext, post_id: str, values: dict[str, str],
                 source: SourceResult | None, recent: list[tuple[str, str]],
                 prior_image: str = "") -> EvalResult:
    """Pure function (no DB or sheet writes): decides the outcome for one row."""
    cfg = ctx.cfg
    composed = compose(values, cfg)
    if not composed.plain:
        return EvalResult(status="Failed", message="متن خالی است: Title، Body یا FullText را پر کنید")
    image_mode = clean(values.get("ImageMode", "")).lower() or "gemini"
    has_image = bool(prior_image) and Path(prior_image).exists()
    inp = PolicyInput(
        post_id=post_id,
        title=clean(values.get("Title", "")),
        body=clean(values.get("Body", "")),
        fulltext=clean(values.get("FullText", "")),
        source_url=clean(values.get("SourceURL", "")),
        hashtags=clean(values.get("Hashtags", "")),
        cta=clean(values.get("CTA", "")),
        image_mode=image_mode,
        image_style=clean(values.get("ImageStyle", "")),
        image_prompt=clean(values.get("ImagePrompt", "")),
        caption_html=composed.html,
        caption_plain=composed.plain,
        has_image=has_image,
    )
    rep = evaluate(inp, PolicyContext(cfg=cfg, banned=ctx.banned, allowed_domains=ctx.allowed_domains,
                                      source=source, recent=recent,
                                      watermark=geministate.watermark_label(ctx.store)))
    rep.changes = composed.changes
    rep.notes = composed.notes
    if rep.verdict == "BLOCK":
        first = rep.blocks[0]
        return EvalResult(status="NeedsFix", message=f"⛔ {first.msg}", report=rep,
                          caption_html=composed.html, caption_plain=composed.plain)
    if image_mode in ("gemini", "auto") and not has_image:
        if ctx.cfg.gemini.enabled_in_phase >= 2:
            return EvalResult(status="Waiting", report=rep, needs_gemini=True,
                              caption_html=composed.html, caption_plain=composed.plain)
        return EvalResult(status="Waiting", report=rep, message=(
            "ساخت تصویر خودکار با Gemini در فاز ۱ فعال نیست. ImageMode را none بگذارید "
            "یا بعد از نصب فاز ۲ دوباره Pending کنید."),
            caption_html=composed.html, caption_plain=composed.plain)
    return EvalResult(status="Drafted", report=rep, caption_html=composed.html,
                      caption_plain=composed.plain,
                      image_path=prior_image if has_image else "", image_mode=image_mode)


def assign_missing_ids(ctx: AppContext, snap: Snapshot) -> list[str]:
    """Give every content row without ID a unique ID (W-0001...). Written to the sheet via outbox."""
    existing = [int(m.group(1)) for r in snap.rows for m in [ID_NUM.match(r.id)] if m]
    new_ids: list[str] = []
    for row in snap.rows:
        if row.id or not any(clean(row.values.get(c, "")) for c in CONTENT_COLS):
            continue
        seq = ctx.store.next_seq("post_seq", at_least=max(existing or [0]))
        nid = f"W-{seq:04d}"
        existing.append(seq)
        ctx.store.outbox_add(nid, "ID", nid, row_hint=row.sheet_row)
        row.values["ID"] = nid
        snap.by_id[nid] = row
        new_ids.append(nid)
    return new_ids


async def fetch_source_cached(ctx: AppContext, url: str) -> SourceResult | None:
    if not url:
        return None
    max_age = ctx.cfg.sources.cache_hours * 3600
    cached = ctx.store.cache_get(url, max_age)
    if cached and cached["ok"]:
        return SourceResult(url=url, domain=cached["domain"], ok=True, text=cached["text"])
    res = await asyncio.to_thread(
        fetch_source, url, ctx.allowed_domains, ctx.cfg.sources.timeout_seconds,
        ctx.cfg.sources.min_text_chars, ctx.env.proxy_url)
    if res.ok:
        ctx.store.cache_put(url, res.domain, res.text, True)
    return res


def _short(msg: str, n: int = 180) -> str:
    return msg if len(msg) <= n else msg[: n - 1] + "…"


async def process_row(ctx: AppContext, notifier: Notifier, values: dict[str, str], *,
                      prior_image: str | None = None, admin_note: str = "", actor: str = "bot",
                      search_terms: str = "") -> Outcome:
    store = ctx.store
    pid = clean(values.get("ID", "")).upper()
    if not pid:
        return Outcome("", "Failed", message="ID ندارد")
    store.create_post(pid)
    post = store.get_post(pid)
    assert post is not None
    if prior_image is None:
        prior_image = post["image_path"] if post["image_path"] and Path(post["image_path"]).exists() else ""

    local = post["status"]
    if local == "Processing":  # crashed mid-way last time
        store.transition(pid, "Processing", "Pending")
        local = "Pending"
    if local == "Failed":
        store.transition(pid, "Failed", "Pending")
        local = "Pending"
    if local in ("Publishing", "Published", "Rejected", "Deleted"):
        return Outcome(pid, "Skipped", message=f"وضعیت پایانی/در حال انتشار: {local}")
    if not store.transition(pid, local, "Processing"):
        return Outcome(pid, "Skipped", message="تغییر وضعیت هم‌زمان؛ رد شد")
    store.set_fields(pid, last_error="")
    store.outbox_add(pid, "Status", "Processing")
    await notifier.progress(pid, 1, "خواندن سطر")

    src_url = clean(values.get("SourceURL", ""))
    source = await fetch_source_cached(ctx, src_url) if src_url else None
    await notifier.progress(pid, 2, "بررسی سیاست‌ها و منبع")
    recent = store.recent_texts(ctx.cfg.policy.duplicate_days, exclude_id=pid)
    ev = evaluate_row(ctx, pid, values, source, recent, prior_image or "")
    if ev.needs_gemini:
        await notifier.progress(pid, 3, "ساخت تصویر با Gemini")
        ev = await gemini_for_row(ctx, pid, values, ev, source, recent, search_terms)
    await notifier.progress(pid, 3, "آماده‌سازی پیش‌نویس")

    summary = ev.report.summary if ev.report else ""
    if ev.status != "Drafted":
        store.transition(pid, "Processing", ev.status)
        store.set_fields(pid, last_error=_short(ev.message), report_json=ev.report.to_json() if ev.report else "")
        store.outbox_add(pid, "Status", ev.status)
        store.outbox_add(pid, "Error", _short(ev.message))
        if summary:
            store.outbox_add(pid, "PolicyReport", summary)
        store.outbox_add(pid, "UpdatedAt", tehran_now())
        store.audit(pid, actor, f"status:{ev.status}", post["version"], ev.message)
        if ev.status == "NeedsFix" and ev.report:
            await notifier.blocked(pid, ev.report)
        elif ev.status == "Waiting":
            await notifier.alert(f"⏸ {pid} منتظر تصویر است.\n{ev.message}")
        elif ev.status == "Failed":
            await notifier.alert(f"⚠️ {pid} ناموفق شد.\n{ev.message}")
        return Outcome(pid, ev.status, post["version"], ev.message)

    new_ver = int(post["version"] or 0) + 1
    h = human_hash(values)
    fh = fulltext_hash(values)
    report_json = ev.report.to_json() if ev.report else ""
    store.set_fields(pid, version=new_ver, human_hash=h, fulltext_hash=fh, caption_html=ev.caption_html,
                     caption_plain=ev.caption_plain, report_json=report_json, image_mode=ev.image_mode,
                     image_path=ev.image_path, stale=0, last_error="", image_file_id="")
    store.add_version(pid, new_ver, ev.caption_html, report_json, h, ev.image_path)
    store.transition(pid, "Processing", "Drafted", version=new_ver)
    note = admin_note.format(ver=new_ver) if admin_note else f"پیش‌نمایش نسخه {new_ver}"
    store.outbox_add(pid, "Status", "Drafted")
    store.outbox_add(pid, "Error", "")
    store.outbox_add(pid, "PolicyReport", summary)
    store.outbox_add(pid, "AdminNote", note)
    store.outbox_add(pid, "UpdatedAt", tehran_now())
    store.audit(pid, actor, "draft", new_ver, summary)
    await notifier.draft_ready(pid)
    return Outcome(pid, "Drafted", new_ver, "")


def wants_gemini(values: dict[str, str], post: dict | None) -> bool:  # type: ignore[type-arg]
    """True when the row asks for an image from Gemini (or search-then-Gemini) and has none yet."""
    mode = clean(values.get("ImageMode", "")).lower() or "gemini"
    has = bool(post and post["image_path"] and Path(post["image_path"]).exists())
    return mode in ("gemini", "auto") and not has


def gemini_lock(ctx: AppContext) -> asyncio.Lock:
    """One browser tab: only one Gemini job at a time (sheet rows, /testimage, /new text)."""
    if ctx.gemini_lock is None:
        ctx.gemini_lock = asyncio.Lock()
    return ctx.gemini_lock


async def _adopt_image(ctx: AppContext, pid: str, values: dict[str, str], ev: EvalResult,
                       source: SourceResult | None, recent: list[tuple[str, str]], raw: Path,
                       kind: str, source_url: str = "") -> EvalResult:
    """Process a raw image (crop, logo, checks), store it, and evaluate the row again with it."""
    post = ctx.store.get_post(pid)
    version = int(post["version"] or 0) + 1 if post else 1
    out = IMAGES_DIR / pid / f"v{version}_{kind}_{raw.stem}.jpg"
    try:
        processed = await asyncio.to_thread(process_image, raw, out, LOGO_PATH, ctx.cfg.images)
    except ImageError as exc:
        return replace(ev, needs_gemini=False, status="Failed",
                       message=f"تصویر قابل استفاده نیست: {exc}")
    recent_hashes = ctx.store.recent_dhashes(ctx.cfg.images.recent_compare)
    if any(hamming(processed.dhash, h) <= ctx.cfg.images.duplicate_hamming for h in recent_hashes):
        out.unlink(missing_ok=True)
        return replace(ev, needs_gemini=False, status="Failed",
                       message="تصویر شبیه تصویرهای اخیر است. سطر را دوباره Pending کنید.")
    ctx.store.add_image(pid, str(out), processed.dhash, kind)
    ctx.store.set_fields(pid, image_path=str(out), image_file_id="")
    ctx.store.audit(pid, "bot", f"image_{kind}", version, out.name)
    if source_url and not clean(values.get("SourceURL", "")):
        values = {**values, "SourceURL": source_url}
        ctx.store.outbox_add(pid, "SourceURL", source_url)  # credit + link for the caption
    return evaluate_row(ctx, pid, values, source, recent, prior_image=str(out))


async def gemini_for_row(ctx: AppContext, pid: str, values: dict[str, str], ev: EvalResult,
                         source: SourceResult | None, recent: list[tuple[str, str]],
                         search_terms: str = "") -> EvalResult:
    """ImageMode=auto: first a licensed photo from Wikimedia Commons, then Gemini. gemini: Gemini only."""
    mode = clean(values.get("ImageMode", "")).lower() or "gemini"
    if mode == "auto" and ctx.cfg.commons.enabled:
        found = await asyncio.to_thread(
            commons.find_photo, search_terms or clean(values.get("Title", "")), IMAGES_DIR / pid / "commons",
            api=ctx.cfg.commons.api, min_side=ctx.cfg.images.min_side, max_results=ctx.cfg.commons.max_results,
            max_bytes=ctx.cfg.admin.max_upload_mb * 1024 * 1024, proxy=ctx.env.proxy_url)
        if found is not None:
            hit, raw = found
            return await _adopt_image(ctx, pid, values, ev, source, recent, raw, "commons", hit.page_url)
    # /pause stops Gemini generation only; a licensed Commons photo above is still allowed
    if geministate.is_paused(ctx.store):
        return replace(ev, needs_gemini=False, status="Waiting",
                       message="تولید تصویر Gemini متوقف است. بعد از /resume دوباره Pending کنید.")
    try:
        new_chat = imagegen.prepare_attempt(ctx)
        prompt = imagegen.build_prompt(ctx.cfg, values)
        async with gemini_lock(ctx):
            raw = await asyncio.to_thread(imagegen.generate_blocking, prompt, IMAGES_DIR / pid / "raw",
                                          ctx.cfg.gemini.timeout_seconds, new_chat)
    except ImageGenError as exc:
        return replace(ev, needs_gemini=False, status=exc.status, message=exc.message)
    except Exception as exc:  # noqa: BLE001
        log.exception("gemini image step failed for %s", pid)
        return replace(ev, needs_gemini=False, status="Failed",
                       message=f"خطای غیرمنتظره در ساخت تصویر: {type(exc).__name__}")
    return await _adopt_image(ctx, pid, values, ev, source, recent, raw, "gemini")


def _next_id(ctx: AppContext, snap: Snapshot) -> str:
    existing = [int(m.group(1)) for r in snap.rows for m in [ID_NUM.match(r.id)] if m]
    return f"W-{ctx.store.next_seq('post_seq', at_least=max(existing or [0])):04d}"


async def new_post_from_brief(ctx: AppContext, notifier: Notifier, brief: str) -> Outcome:
    """Telegram /new: Gemini writes the text, the row is appended to the sheet, then the normal pipeline runs."""
    try:
        gemini_post.check_ready(ctx)
    except gemini_post.PostError as exc:
        return Outcome("", "Failed", message=str(exc))
    fields, reason = None, ""
    for variant in ("structured", "plain"):  # if the answer is unusable, try once with the plainer wording
        try:
            gemini_post.spend_text_budget(ctx)
            async with gemini_lock(ctx):
                text = await asyncio.to_thread(gemini_post.write_post_blocking, brief,
                                               ctx.cfg.gemini.timeout_seconds, variant)
        except gemini_post.PostError as exc:
            return Outcome("", "Failed", message=str(exc))
        try:
            fields = gemini_post.parse_post(text)
            break
        except gemini_post.PostError as exc:
            reason = str(exc)
    if fields is None:
        return Outcome("", "Failed", message=f"{reason} موضوع را بازنویسی کنید و دوباره اضافه کنید.")
    if ctx.lock is None:
        ctx.lock = asyncio.Lock()
    async with ctx.lock:
        snap = await asyncio.to_thread(ctx.sheet.read_content)
        pid = _next_id(ctx, snap)
        values = {c: "" for c in COLUMNS}
        values.update({
            "ID": pid, "Status": "Pending", "Title": fields["TITLE"], "Body": fields["BODY"],
            "Hashtags": fields["HASHTAGS"], "CTA": fields["CTA"], "ImageMode": "auto", "ImageStyle": "photo",
            "AdminNote": f"ساخته‌شده از تلگرام: {' '.join(brief.split())[:150]}",
        })
        await asyncio.to_thread(ctx.sheet.append_row, values)
        return await process_row(ctx, notifier, values, actor="admin", search_terms=fields["KEYWORDS"])


async def check_stale(ctx: AppContext, notifier: Notifier, snap: Snapshot) -> list[str]:
    """Drafted posts whose sheet content changed by a human become stale (old previews dead)."""
    marked: list[str] = []
    for post in ctx.store.posts_with_status("Drafted"):
        if post["stale"] or ctx.store.outbox_for(post["id"]):
            continue
        row = snap.find(post["id"])
        if row is None:
            continue
        reason = ""
        if human_hash(row.values) != post["human_hash"]:
            reason = "محتوا در شیت تغییر کرد"
        elif row.status not in ("Drafted", ""):
            reason = f"وضعیت شیت تغییر کرد ({row.status})"
        if reason:
            ctx.store.set_fields(post["id"], stale=1)
            ctx.store.audit(post["id"], "bot", "stale", post["version"], reason)
            marked.append(post["id"])
            await notifier.stale(post["id"], post["version"], reason)
    return marked


async def sync_once(ctx: AppContext, notifier: Notifier) -> dict[str, int]:
    """One poll of the sheet: assign IDs, detect stale drafts, process Pending rows top-down, flush writes."""
    stats = {"pending": 0, "processed": 0, "stale": 0, "paused": 0}
    try:
        snap = await asyncio.to_thread(ctx.sheet.read_content)
    except Exception as exc:  # noqa: BLE001
        now = time.time()
        if not ctx.sheet_fail_since:
            ctx.sheet_fail_since = now
        if now - ctx.sheet_fail_since >= 600 and not ctx.sheet_alerted:
            ctx.sheet_alerted = True
            await notifier.alert("🔴 شیت گوگل بیش از ۱۰ دقیقه در دسترس نبوده است. "
                                 f"علت: {_short(str(exc), 120)}")
        log.warning("sheet read failed: %s", str(exc)[:200])
        return stats
    if ctx.sheet_fail_since:
        if ctx.sheet_alerted:
            await notifier.alert(f"📶 اتصال به شیت برگشت؛ {ctx.store.outbox_count()} مورد در صف است.")
        ctx.sheet_fail_since = 0.0
        ctx.sheet_alerted = False

    assign_missing_ids(ctx, snap)
    stats["stale"] = len(await check_stale(ctx, notifier, snap))

    max_drafts = ctx.cfg.sheet.max_pending_drafts
    paused = geministate.is_paused(ctx.store)
    for row in snap.rows:
        if row.status != "Pending" or not row.id:
            continue
        stats["pending"] += 1
        local = ctx.store.get_post(row.id)
        if paused and wants_gemini(row.values, local):
            stats["paused"] = stats.get("paused", 0) + 1
            continue  # stays Pending until /resume
        already_drafted = bool(local and local["status"] == "Drafted")
        if not already_drafted and ctx.store.count_status("Drafted") >= max_drafts:
            log.info("سقف پیش‌نویس‌ها پر است؛ %s در صف می‌ماند", row.id)
            break
        await process_row(ctx, notifier, row.values)
        stats["processed"] += 1
    await flush_now(ctx)
    ctx.store.kv_set("last_sync", str(time.time()))
    return stats


async def flush_now(ctx: AppContext) -> tuple[int, int]:
    return await asyncio.to_thread(flush_outbox, ctx.store, ctx.sheet, time.time())


def write_updates(ctx: AppContext, pid: str, updates: list[tuple[str, str]]) -> None:
    for col, val in updates:
        if col not in COLUMNS:
            raise ValueError(f"ستون نامعتبر: {col}")
        ctx.store.outbox_add(pid, col, val)


async def redraft(ctx: AppContext, notifier: Notifier, pid: str, *, admin_note: str = "",
                  actor: str = "bot", values: dict[str, str] | None = None) -> Outcome:
    if values is None:
        snap = await asyncio.to_thread(ctx.sheet.read_content)
        row = snap.find(pid)
        if row is None:
            return Outcome(pid, "Failed", message="سطر در شیت پیدا نشد")
        values = dict(row.values)
        values.setdefault("ID", pid)
    return await process_row(ctx, notifier, values, admin_note=admin_note, actor=actor)


async def edit_text(ctx: AppContext, notifier: Notifier, pid: str, text_html: str, actor: str) -> Outcome | str:
    """Admin edited the text in Telegram. Returns 'conflict' if the sheet FullText changed too."""
    snap = await asyncio.to_thread(ctx.sheet.read_content)
    row = snap.find(pid)
    if row is None:
        return Outcome(pid, "Failed", message="سطر در شیت پیدا نشد")
    post = ctx.store.get_post(pid)
    assert post is not None
    if fulltext_hash(row.values) != post["fulltext_hash"] and clean(row.values.get("FullText", "")) != text_html:
        return "conflict"
    await _write_fulltext(ctx, pid, text_html)
    ctx.store.set_fields(pid, edit_count=int(post["edit_count"] or 0) + 1)
    ctx.store.audit(pid, actor, "edit_telegram", post["version"], f"{len(text_html)} کاراکتر")
    return await redraft(ctx, notifier, pid, admin_note="ویرایش در تلگرام، نسخه {ver}", actor=actor)


async def _write_fulltext(ctx: AppContext, pid: str, text_html: str) -> None:
    write_updates(ctx, pid, [("FullText", text_html)])
    await flush_now(ctx)


async def write_fulltext_and_redraft(ctx: AppContext, notifier: Notifier, pid: str, text_html: str,
                                     actor: str) -> Outcome:
    """Conflict resolved in favour of Telegram."""
    await _write_fulltext(ctx, pid, text_html)
    return await redraft(ctx, notifier, pid, admin_note="ویرایش در تلگرام، نسخه {ver}", actor=actor)


async def upload_image(ctx: AppContext, notifier: Notifier, pid: str, src: Path, actor: str) -> Outcome:
    """Admin sent a photo: crop 16:9, add logo, reject duplicates/blank, then redraft with it."""
    post = ctx.store.get_post(pid)
    if post is None:
        return Outcome(pid, "Failed", message="پست پیدا نشد")
    k = int(post["upload_count"] or 0) + 1
    out = IMAGES_DIR / pid / f"v{int(post['version'] or 0) + 1}_upload_{k}.jpg"
    processed = await asyncio.to_thread(process_image, src, out, LOGO_PATH, ctx.cfg.images)
    recent = ctx.store.recent_dhashes(ctx.cfg.images.recent_compare)
    if any(hamming(processed.dhash, h) <= ctx.cfg.images.duplicate_hamming for h in recent):
        out.unlink(missing_ok=True)
        raise ImageError("این تصویر با یکی از تصاویر اخیر تکراری است. عکس دیگری بفرستید.")
    ctx.store.add_image(pid, str(out), processed.dhash, "upload")
    ctx.store.set_fields(pid, image_path=str(out), upload_count=k, image_file_id="")
    ctx.store.audit(pid, actor, "upload_image", post["version"], out.name)
    return await redraft(ctx, notifier, pid, admin_note="عکس آپلودی، نسخه {ver}", actor=actor)


def dry_run(ctx: AppContext, pid: str, values: dict[str, str], source: SourceResult | None) -> EvalResult:
    """Used by --dry-run: prints what would happen, writes nothing."""
    return evaluate_row(ctx, pid, values, source, recent=[], prior_image="")


__all__ = ["process_row", "sync_once", "redraft", "edit_text", "upload_image", "dry_run",
           "evaluate_row", "tehran_now", "gemini_for_row", "wants_gemini", "new_post_from_brief", "NullNotifier", "Outcome", "EvalResult", "strip_html", "Row"]
