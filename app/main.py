"""Entry point.

  python -m app.main            run the bot (after pre-flight checks)
  python -m app.main --check    pre-flight checks with ✅/❌
  python -m app.main --dry-run W-0001   show what the bot would draft for a row (writes nothing)
  python -m app.main --version
"""
from __future__ import annotations

import asyncio
import atexit
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import (CONFIG_PATH, DB_PATH, LOCK_PATH, LOG_PATH, VERSION, Config, ensure_dirs,
                     load_config, load_env)
from .logsetup import setup_logging



def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> bool:
    if LOCK_PATH.exists():
        try:
            old = int(LOCK_PATH.read_text().strip() or "0")
        except ValueError:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            return False
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK_PATH.unlink(missing_ok=True))
    return True


def _load_cfg() -> tuple[Config | None, str]:
    try:
        return load_config(CONFIG_PATH), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def check_cli() -> int:
    from .health import format_checks, run_checks
    cfg, err = _load_cfg()
    env = load_env()
    checks = asyncio.run(run_checks(env, cfg, err))
    print(format_checks(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def run_cli() -> int:
    from telegram import Update

    from .context import build_context
    from .health import format_checks, run_checks
    from .store import Store
    from .telegram_bot import build_application

    ensure_dirs()
    cfg, err = _load_cfg()
    env = load_env()
    if cfg is None:
        print(f"❌ فایل تنظیمات خوانده نشد: {err}")
        return 1
    setup_logging(LOG_PATH, cfg.logging.max_mb, cfg.logging.backups)
    from .housekeeping import run_housekeeping
    run_housekeeping(cfg, DB_PATH)
    checks = asyncio.run(run_checks(env, cfg, err, critical_only=True))
    print(format_checks(checks))
    if any(c.status == "fail" for c in checks):
        print("\n❌ ربات شروع نشد. موارد بالا را اصلاح کنید.")
        return 1
    if not acquire_lock():
        print("❌ ربات جای دیگری در حال اجراست. اگر مطمئن هستید، پنجره‌ی قبلی را ببندید.")
        return 1
    store = Store(DB_PATH)
    ctx = build_context(cfg, env, store)
    app = build_application(ctx)
    print("🟢 ربات در حال اجراست. این پنجره را باز نگه دارید. برای توقف Ctrl+C بزنید.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    store.close()
    return 0


def dry_run_cli(post_id: str) -> int:
    from .context import AppContext, build_context, reload_rules
    from .pipeline import dry_run
    from .sources import fetch_source
    from .store import Store

    cfg, err = _load_cfg()
    if cfg is None:
        print(f"❌ {err}")
        return 1
    env = load_env()
    ensure_dirs()
    tmp = Path(tempfile.mkdtemp()) / "dry.db"
    store = Store(tmp)
    ctx: AppContext = build_context(cfg, env, store)
    reload_rules(ctx)
    snap = ctx.sheet.read_content()
    row = snap.find(post_id.upper())
    if row is None:
        print(f"❌ {post_id} در شیت پیدا نشد.")
        return 1
    url = row.values.get("SourceURL", "").strip()
    source = fetch_source(url, ctx.allowed_domains, cfg.sources.timeout_seconds,
                          cfg.sources.min_text_chars, env.proxy_url) if url else None
    ev = dry_run(ctx, post_id.upper(), row.values, source)
    print(f"وضعیت پیش‌بینی‌شده: {ev.status}")
    if ev.message:
        print(f"پیام: {ev.message}")
    if ev.report:
        print(ev.report.render(version=None))
    if ev.caption_plain:
        print("\n----- کپشن -----")
        print(ev.caption_plain)
    store.close()
    return 0


HELP_TEXT = """\
ایجنت انتشار وال (فاز ۲)
  python -m app.main                  اجرای ربات
  python -m app.main --check          بررسی تنظیمات قبل از اجرا
  python -m app.main --dry-run W-0001 شبیه‌سازی یک ردیف بدون ارسال به کانال
  python -m app.main --version        نمایش نسخه
  python -m app.main --help           همین راهنما"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    if "--version" in args:
        print(VERSION)
        return 0
    if "--help" in args or "-h" in args:
        print(HELP_TEXT)
        return 0
    if "--login" in args:
        from .config import load_env
        from .gemini import login_cli
        return login_cli(load_env().chrome_path)
    if "--inspect" in args:
        from .gemini import inspect_cli
        return inspect_cli()
    if "--check" in args:
        return check_cli()
    try:
        if "--dry-run" in args:
            i = args.index("--dry-run")
            if i + 1 >= len(args):
                print("مثال: python -m app.main --dry-run W-0001")
                return 1
            return dry_run_cli(args[i + 1])
        return run_cli()
    except RuntimeError as exc:  # configuration problems raised by build_context
        print(f"❌ {exc}")
        print("ابتدا این دستور را اجرا کنید تا موارد ناقص مشخص شود: python scripts/check.py")
        return 1


if __name__ == "__main__":
    sys.exit(main())
