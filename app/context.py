"""Shared application context: config, env, store, sheet client and cached rules."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import BANNED_TXT_PATH, SHEET_DIR, Config, Env
from .localsheet import LocalBackend
from .sheets import COLUMNS, GSpreadBackend, SheetClient
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    cfg: Config
    env: Env
    store: Store
    sheet: SheetClient
    banned: list[tuple[str, str]] = field(default_factory=list)
    allowed_domains: set[str] = field(default_factory=set)
    sheet_fail_since: float = 0.0
    sheet_alerted: bool = False
    lock: asyncio.Lock | None = None
    gemini_lock: asyncio.Lock | None = None
    started_at: float = 0.0


def parse_banned_txt(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word, _, level = line.partition("|")
        level = level.strip().lower() or "hard"
        if level not in ("hard", "soft"):
            log.warning("سطح نامعتبر در banned_words: %s — به hard تنظیم شد", line)
            level = "hard"
        out.append((word.strip(), level))
    return out


def reload_rules(ctx: AppContext) -> list[str]:
    """Reload banned words (sheet tab first, txt fallback) and allowed source domains."""
    notes: list[str] = []
    tab_words = ctx.sheet.read_banned()
    if tab_words is None:
        ctx.banned = parse_banned_txt(BANNED_TXT_PATH)
        notes.append("فهرست کلمات ممنوع از فایل config/banned_words.txt خوانده شد")
    else:
        ctx.banned = [(w, lvl if lvl in ("hard", "soft") else "hard") for w, lvl in tab_words]
        notes.append("فهرست کلمات ممنوع از تب BannedWords خوانده شد")
    domains = ctx.sheet.read_sources()
    if domains is None:
        ctx.allowed_domains = {d.lower() for d in ctx.cfg.sources.allowed_domains_fallback}
        notes.append("تب Sources پیدا نشد؛ فهرست پیش‌فرض دامنه‌ها استفاده شد")
    else:
        ctx.allowed_domains = set(domains)
    return notes


def make_backend(cfg: Config, env: Env):  # noqa: ANN201
    """Local CSV queue by default. Google Sheets is used only when sheet.backend is "google"."""
    if cfg.sheet.backend == "google":
        if not env.sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID در فایل .env خالی است")
        return GSpreadBackend(env.service_account_file, env.sheet_id)
    if cfg.sheet.backend != "local":
        raise RuntimeError(f"sheet.backend نامعتبر است: {cfg.sheet.backend!r} (local یا google)")
    backend = LocalBackend(SHEET_DIR)
    backend.ensure_tab(cfg.sheet.tab_content, COLUMNS)  # first run: create the queue with the 18 headers
    return backend


def build_context(cfg: Config, env: Env, store: Store, backend=None) -> AppContext:  # noqa: ANN001
    if backend is None:
        backend = make_backend(cfg, env)
    sheet = SheetClient(backend, cfg.sheet.tab_content, cfg.sheet.tab_banned, cfg.sheet.tab_sources)
    ctx = AppContext(cfg=cfg, env=env, store=store, sheet=sheet)
    return ctx
