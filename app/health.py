"""Pre-flight checks (python -m app.main --check). Each failure has a Persian fix hint."""
from __future__ import annotations

import sys
from dataclasses import dataclass

from .config import BANNED_TXT_PATH, DATA_DIR, LOGO_PATH, Config, Env
from .images import check_logo


@dataclass
class Check:
    name: str
    status: str  # ok | fail | warn | info
    msg: str = ""
    fix: str = ""
    critical: bool = True

    @property
    def icon(self) -> str:
        return {"ok": "✅", "fail": "❌", "warn": "⚠️", "info": "ℹ️"}[self.status]


def check_python() -> Check:
    v = sys.version_info
    if (3, 11) <= (v.major, v.minor) <= (3, 15):
        return Check("نسخه‌ی Python", "ok", f"{v.major}.{v.minor}.{v.micro}")
    return Check("نسخه‌ی Python", "fail", f"{v.major}.{v.minor}",
                 "Python 3.13 (یا ۳.۱۱ تا ۳.۱۵) نصب کنید و run.bat را دوباره اجرا کنید.")


def check_env(env: Env, cfg: Config | None = None) -> list[Check]:
    out: list[Check] = []
    out.append(Check("TELEGRAM_BOT_TOKEN", "ok" if env.telegram_token else "fail",
                     "پر است" if env.telegram_token else "خالی است",
                     "توکن ربات را از BotFather در فایل .env بگذارید."))
    out.append(Check("ADMIN_USER_ID", "ok" if env.admin_ok else "fail",
                     str(env.admin_user_id) if env.admin_ok else "نامعتبر یا خالی",
                     "عدد آی‌دی تلگرام خودتان را (فقط عدد) در ADMIN_USER_ID بگذارید."))
    out.append(Check("CHANNEL_ID", "ok" if env.channel_id else "fail", env.channel_id,
                     "یوزرنیم کانال مثل @waltest0001 یا آی‌دی -100... را بگذارید."))
    if cfg is not None and cfg.sheet.backend == "local":
        return out  # local queue: no Google settings needed
    out.append(Check("GOOGLE_SHEET_ID", "ok" if env.sheet_id else "fail",
                     "پر است" if env.sheet_id else "خالی است",
                     "شناسه‌ی شیت را از آدرس شیت (بین /d/ و /edit) کپی کنید."))
    sa_ok = env.service_account_file.exists()
    out.append(Check("فایل سرویس‌اکانت", "ok" if sa_ok else "fail", str(env.service_account_file.name),
                     "فایل JSON سرویس‌اکانت را در پوشه‌ی secrets با نام service_account.json بگذارید."))
    return out


def check_config(cfg: Config | None, error: str = "") -> Check:
    if cfg is None:
        return Check("config/config.json", "fail", error[:150], "فایل config/config.json را دوباره از روی نسخه‌ی اصلی بسازید.")
    return Check("config/config.json", "ok", f"نسخه {cfg.version}")


def check_logo_file() -> Check:
    err = check_logo(LOGO_PATH)
    if err:
        return Check("لوگو (assets/logo.png)", "fail", err, "لوگوی شفاف PNG را با نام logo.png در پوشه‌ی assets بگذارید.")
    return Check("لوگو (assets/logo.png)", "ok", "شفاف است")


def check_banned_file() -> Check:
    if BANNED_TXT_PATH.exists():
        return Check("فهرست کلمات ممنوع (txt)", "ok", "موجود است", critical=False)
    return Check("فهرست کلمات ممنوع (txt)", "warn", "فایل پیدا نشد؛ اگر تب BannedWords هم نباشد، فیلتر خالی است.",
                 "فایل config/banned_words.txt را بسازید یا تب BannedWords را در شیت بسازید.", critical=False)


def check_data_dir() -> Check:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check("پوشه‌ی data (نوشتن)", "ok", "قابل نوشتن است")
    except OSError as exc:
        return Check("پوشه‌ی data (نوشتن)", "fail", str(exc)[:120], "دسترسی نوشتن در پوشه‌ی پروژه را بررسی کنید.")


def check_sheet(env: Env, cfg: Config) -> list[Check]:
    from .context import make_backend
    from .sheets import COLUMNS, GSpreadBackend, SheetClient, missing_headers

    if cfg.sheet.backend == "local":
        try:
            backend = make_backend(cfg, env)
            table = backend.read_tab(cfg.sheet.tab_content)
        except Exception as exc:  # noqa: BLE001
            return [Check("صف محلی", "fail", str(exc)[:160], "پوشه‌ی data را بررسی کنید و فایل‌های CSV را در Excel نبندید.")]
        client = SheetClient(backend, cfg.sheet.tab_content, cfg.sheet.tab_banned, cfg.sheet.tab_sources)
        checks: list[Check] = [Check("صف محلی (CSV)", "ok", str(backend.folder))]
    elif not env.sheet_id or not env.service_account_file.exists():
        return [Check("اتصال به شیت", "fail", "تنظیمات شیت کامل نیست", "ابتدا بخش‌های شیت و سرویس‌اکانت را در .env پر کنید.")]
    else:
        try:
            backend = GSpreadBackend(env.service_account_file, env.sheet_id)
            client = SheetClient(backend, cfg.sheet.tab_content, cfg.sheet.tab_banned, cfg.sheet.tab_sources)
            table = backend.read_tab(cfg.sheet.tab_content)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            fix = "شیت را با ایمیل سرویس‌اکانت (client_email داخل JSON) به‌عنوان Editor Share کنید و Google Sheets API را فعال کنید."
            if "403" in text or "PERMISSION" in text.upper():
                fix = "دسترسی شیت به سرویس‌اکانت داده نشده است. ایمیل سرویس‌اکانت را در Share با نقش Editor اضافه کنید."
            if "404" in text or "NOT_FOUND" in text.upper():
                fix = "شناسه‌ی شیت (GOOGLE_SHEET_ID) اشتباه است."
            return [Check("اتصال به شیت", "fail", text[:160], fix)]
        checks = [Check("اتصال به شیت", "ok", "برقرار است")]
    if table is None:
        checks.append(Check("تب Content", "fail", "پیدا نشد", "اسکریپت scripts/init_sheet.py را اجرا کنید."))
        return checks
    headers = table[0] if table else []
    miss = missing_headers(headers, COLUMNS)
    if miss:
        checks.append(Check("هدرهای تب Content", "fail", "ناقص: " + "، ".join(miss),
                            "scripts/init_sheet.py را اجرا کنید تا هدرها اضافه شوند."))
    else:
        checks.append(Check("هدرهای تب Content", "ok", f"{len(COLUMNS)} ستون کامل است"))
    if client.read_banned() is None:
        checks.append(Check("تب BannedWords", "warn", "پیدا نشد؛ از فایل txt استفاده می‌شود", "اختیاری است.", critical=False))
    else:
        checks.append(Check("تب BannedWords", "ok", "موجود است", critical=False))
    if client.read_sources() is None:
        checks.append(Check("تب Sources", "warn", "پیدا نشد؛ فهرست پیش‌فرض دامنه‌ها استفاده می‌شود",
                            "برای مدیریت دامنه‌های منبع، scripts/init_sheet.py را اجرا کنید.", critical=False))
    else:
        checks.append(Check("تب Sources", "ok", "موجود است", critical=False))
    return checks


async def check_telegram(env: Env) -> list[Check]:
    from telegram import Bot
    from telegram.error import TelegramError
    from telegram.request import HTTPXRequest

    from .publisher import check_channel_rights

    if not env.telegram_token:
        return [Check("اتصال به تلگرام", "fail", "توکن خالی است", "توکن را در .env بگذارید.")]
    # Use the same proxy as the bot itself, so the check reflects real connectivity.
    request = HTTPXRequest(proxy=env.proxy_url) if env.proxy_url else HTTPXRequest()
    bot = Bot(env.telegram_token, request=request)
    try:
        me = await bot.get_me()
    except TelegramError as exc:
        return [Check("اتصال به تلگرام", "fail", str(exc)[:150],
                      "توکن ربات را بررسی کنید (از BotFather دوباره بگیرید اگر لازم است).")]
    checks = [Check("اتصال به تلگرام", "ok", f"@{me.username}")]
    if env.channel_id:
        problems = await check_channel_rights(bot, env.channel_id)
        if problems:
            checks.append(Check("دسترسی ربات در کانال", "fail", " | ".join(problems),
                                "ربات را در کانال ادمین کنید و دسترسی‌های ارسال، ویرایش و حذف را بدهید."))
        else:
            checks.append(Check("دسترسی ربات در کانال", "ok", "ادمین است و سه دسترسی را دارد"))
    return checks


def check_phase2(env: Env | None = None) -> list[Check]:
    """Gemini prerequisites. Never fatal: the bot runs without them (ImageMode=none still works)."""
    from . import gemini
    checks: list[Check] = []
    try:
        checks.append(Check("Google Chrome (برای Gemini)", "ok", gemini.find_chrome(env.chrome_path if env else ""),
                            critical=False))
    except gemini.GeminiError as exc:
        checks.append(Check("Google Chrome (برای Gemini)", "warn", str(exc)[:150],
                            "CHROME_PATH را در .env بگذارید.", critical=False))
    try:
        gemini.require_verified(gemini.load_selectors())
        checks.append(Check("سلکتورهای Gemini", "ok", "تأیید شده", critical=False))
    except gemini.GeminiError as exc:
        checks.append(Check("سلکتورهای Gemini", "warn", str(exc)[:150],
                            "python -m app.main --login ، سپس --inspect", critical=False))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("سلکتورهای Gemini", "warn", f"فایل خوانده نشد: {type(exc).__name__}",
                            "config/gemini_selectors.yaml را بررسی کنید.", critical=False))
    return checks


def check_playwright(env: Env) -> Check:
    import importlib.util
    if importlib.util.find_spec("playwright") is None:
        return Check("Chromium برای رندر منبع", "warn", "کتابخانه نصب نیست (اختیاری)",
                     "اگر منبع‌های JS نیاز داشتید: python -m playwright install chromium", critical=False)
    return Check("Chromium برای رندر منبع", "ok", "کتابخانه نصب است (اختیاری)", critical=False)


async def run_checks(env: Env, cfg: Config | None, cfg_error: str = "", critical_only: bool = False) -> list[Check]:
    checks: list[Check] = [check_python(), check_config(cfg, cfg_error), check_data_dir(), check_logo_file()]
    checks += check_env(env, cfg)
    checks.append(check_banned_file())
    if cfg is not None:
        checks += check_sheet(env, cfg)
    checks += await check_telegram(env)
    checks.append(check_playwright(env))
    checks += check_phase2(env)
    if critical_only:
        checks = [c for c in checks if c.critical or c.status == "fail"]
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{c.icon} {c.name}: {c.msg}")
        if c.status in ("fail", "warn") and c.fix:
            lines.append(f"    ↳ راه‌حل: {c.fix}")
    failed = [c for c in checks if c.status == "fail"]
    lines.append("")
    lines.append("همه‌چیز آماده است." if not failed else f"{len(failed)} مورد نیاز به اصلاح دارد.")
    return "\n".join(lines)

