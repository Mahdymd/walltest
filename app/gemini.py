"""Phase 2 browser layer: Chrome over CDP, locked to gemini.google.com.

Safety rules (spec section 1.1):
- The bot never types credentials. The admin logs in by hand in the Chrome window it opens.
- The browser never navigates away from gemini.google.com (checked before every action).
- Image generation is refused until config/gemini_selectors.yaml is marked "verified"
  by a real --inspect run on the admin's machine.
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ruamel.yaml import YAML

from .config import ROOT

log = logging.getLogger("wal.gemini")

GEMINI_HOST = "gemini.google.com"
GEMINI_URL = f"https://{GEMINI_HOST}/app"
SELECTORS_PATH = ROOT / "config" / "gemini_selectors.yaml"
PROFILE_DIR = ROOT / "data" / "chrome_profile"
DEFAULT_DEBUG_PORT = 9222
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


class GeminiError(Exception):
    """Raised for any browser-side problem that should be shown to the admin in Persian."""


def is_gemini_url(url: str) -> bool:
    """True only for https URLs whose host is exactly gemini.google.com."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == GEMINI_HOST


def assert_on_gemini(url: str) -> None:
    if not is_gemini_url(url):
        raise GeminiError(f"مرورگر خارج از Gemini است ({urlparse(url).hostname or 'نامشخص'}). عملیات متوقف شد.")


@dataclass
class Selectors:
    status: str
    elements: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def load_selectors(path: Path = SELECTORS_PATH) -> Selectors:
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("elements") or {}
    elements = {k: str(v.get("css", "")).strip() for k, v in raw.items() if isinstance(v, dict)}
    return Selectors(status=str(data.get("status", "needs_verification")), elements=elements,
                     note=str(data.get("note", "")))


def require_verified(sel: Selectors) -> None:
    missing = [k for k in ("prompt_box", "send_button", "image_result") if not sel.elements.get(k)]
    if not sel.verified or missing:
        raise GeminiError("سلکتورهای Gemini هنوز تأیید نشده‌اند. اول python -m app.main --inspect را اجرا کنید.")


def find_chrome(configured: str = "") -> str:
    for candidate in [configured, *CHROME_CANDIDATES]:
        if candidate and Path(candidate).exists():
            return candidate
    raise GeminiError("Chrome پیدا نشد. مسیر آن را در CHROME_PATH داخل .env بگذارید.")


def login_command(chrome: str, port: int = DEFAULT_DEBUG_PORT, profile: Path = PROFILE_DIR) -> list[str]:
    return [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", GEMINI_URL]


def login_cli(chrome_path: str = "", port: int = DEFAULT_DEBUG_PORT) -> int:
    try:
        chrome = find_chrome(chrome_path)
    except GeminiError as exc:
        print(f"❌ {exc}")
        return 1
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(login_command(chrome, port), close_fds=True)
    print("🌐 Chrome باز شد. مراحل زیر را انجام دهید:")
    print("  ۱. با حساب Google خودتان وارد Gemini شوید (رمز را فقط داخل Chrome وارد کنید).")
    print("  ۲. وقتی صفحه‌ی گفتگوی Gemini باز شد، پنجره‌ی Chrome را باز نگه دارید.")
    print("  ۳. در این پنجره‌ی جدید: python -m app.main --inspect")
    return 0


def _connect(port: int):  # type: ignore[no-untyped-def]
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        raise GeminiError("به Chrome متصل نشدم. آیا پنجره‌ی Chrome از دستور --login باز است؟") from exc
    return pw, browser


def _gemini_page(browser):  # type: ignore[no-untyped-def]
    for context in browser.contexts:
        for page in context.pages:
            if is_gemini_url(page.url):
                return page
    raise GeminiError("صفحه‌ی Gemini در Chrome پیدا نشد. Chrome را از --login باز کنید و صفحه‌ی Gemini را نگه دارید.")



# Broad candidate queries used only for discovery; they are never used for generation.
CANDIDATE_QUERIES = {
    "prompt_box": '[contenteditable="true"], textarea',
    "send_button": 'button[aria-label*="Send" i], button[aria-label*="ارسال"], button[type="submit"]',
    "image_result": 'img[src^="https://"]',
    "response_text": 'message-content, model-response, .model-response-text',
}
_DESCRIBE_JS = (
    "els => els.slice(0, 5).map(e => ({tag: e.tagName, aria: e.getAttribute('aria-label') || '', "
    "role: e.getAttribute('role') || '', ce: e.getAttribute('contenteditable') || '', "
    "ph: e.getAttribute('placeholder') || '', "
    "cls: (typeof e.className === 'string' ? e.className : '').split(' ').filter(Boolean).slice(0, 3).join(' ')}))"
)


def suggest_css(d: dict) -> str:
    """Build a conservative selector from technical attributes only (never from text content)."""
    tag = str(d.get("tag", "")).lower() or "*"
    if d.get("aria"):
        return tag + '[aria-label="' + str(d["aria"]) + '"]'
    if d.get("ce") == "true":
        return tag + '[contenteditable="true"]'
    if d.get("ph"):
        return tag + '[placeholder="' + str(d["ph"]) + '"]'
    if d.get("role"):
        return tag + '[role="' + str(d["role"]) + '"]'
    return tag


def print_candidates(page) -> None:  # type: ignore[no-untyped-def]
    print("\nکاندیدهای سلکتور (فقط ویژگی‌های فنی):")
    for name, query in CANDIDATE_QUERIES.items():
        total = page.locator(query).count()
        print(f"  • {name}: {total} مورد")
        if not total:
            continue
        for d in page.eval_on_selector_all(query, _DESCRIBE_JS)[:3]:
            attrs = " | ".join(f"{k}={v}" for k, v in d.items() if v)
            print(f"      - {attrs}")
            print(f"        پیشنهاد: {suggest_css(d)}")


# Technical facts only: size, class and place of each image. No src, no alt, no text.
_IMAGE_DIAG_JS = (
    "els => els.slice(0, 12).map(e => { const r = e.getBoundingClientRect(); return {"
    "tag: e.tagName, cls: (typeof e.className === 'string' ? e.className : '').split(' ').filter(Boolean).slice(0, 3).join(' '), "
    "src_scheme: (e.getAttribute('src') || '').split(':')[0], "
    "shown_w: Math.round(r.width), shown_h: Math.round(r.height), "
    "natural_w: e.naturalWidth, natural_h: e.naturalHeight, "
    "parent: e.parentElement ? e.parentElement.tagName : '', "
    "in_model_response: !!e.closest('model-response')}; })"
)


def inspect_cli(port: int = DEFAULT_DEBUG_PORT) -> int:
    """Count how many elements each selector matches on the open Gemini page. Changes nothing."""
    sel = load_selectors()
    if not sel.elements:
        print("❌ هیچ سلکتوری در config/gemini_selectors.yaml تعریف نشده است.")
        return 1
    try:
        pw, browser = _connect(port)
    except GeminiError as exc:
        print(f"❌ {exc}")
        return 1
    try:
        page = _gemini_page(browser)
        assert_on_gemini(page.url)
        print(f"✅ صفحه‌ی Gemini پیدا شد: {urlparse(page.url).hostname}")
        for name, css in sel.elements.items():
            if not css:
                print(f"  ⚪ {name}: خالی (باید پر شود)")
                continue
            count = page.locator(css).count()
            mark = "✅" if count > 0 else "❌"
            print(f"  {mark} {name}: {count} مورد پیدا شد")
        print_candidates(page)
        print("\nتصویرهای صفحه (فقط اندازه و جایگاه):")
        for d in page.eval_on_selector_all("img", _IMAGE_DIAG_JS):
            print("  - " + " | ".join(f"{k}={v}" for k, v in d.items()))
        print("\nاین خروجی فقط ویژگی‌های فنی را نشان می‌دهد (بدون متن گفتگو). آن را برای تأیید سلکتورها بفرستید.")
        print("اگر همه‌ی موارد لازم ✅ هستند، فایل config/gemini_selectors.yaml را با status: verified علامت بزنید.")
        return 0
    except GeminiError as exc:
        print(f"❌ {exc}")
        return 1
    finally:
        pw.stop()  # do not close the browser: it is the admin's own Chrome window


_BLOB_JS = (
    "async (u) => { const b = await (await fetch(u)).blob(); return await new Promise((res, rej) => {"
    " const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(r.error);"
    " r.readAsDataURL(b); }); }"
)


_GOOGLE_SIZE_SUFFIX = re.compile(r"=(?:s|w|h)\d+(?:-[A-Za-z0-9]+)*$")


def original_image_url(src: str) -> str:
    """Google image URLs end with a size such as =s512 (a small preview). =s0 asks for the original size."""
    host = urlparse(src).hostname or ""
    if host.endswith("googleusercontent.com") and _GOOGLE_SIZE_SUFFIX.search(src):
        return _GOOGLE_SIZE_SUFFIX.sub("=s0", src)
    return src


def _image_bytes(page, src: str) -> bytes | None:  # type: ignore[no-untyped-def]
    """Original bytes for an https image or a blob that belongs to Gemini; None means 'use a screenshot'."""
    if src.startswith("https://"):
        return page.request.get(original_image_url(src)).body()
    if src.startswith("blob:") and is_gemini_url(src[len("blob:"):]):
        try:
            data_url = page.evaluate(_BLOB_JS, src)
        except Exception:  # noqa: BLE001 - e.g. "Failed to fetch": fall back to the screenshot
            return None
        if isinstance(data_url, str) and "," in data_url:
            return base64.b64decode(data_url.split(",", 1)[1])
    return None


_CANVAS_JS = ("img => { if (!img.complete || !img.naturalWidth) return null; "
              "const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight; "
              "c.getContext('2d').drawImage(img, 0, 0); return c.toDataURL('image/png'); }")


def _canvas_bytes(target) -> bytes | None:  # type: ignore[no-untyped-def]
    """Draw the loaded image on a canvas: full natural size, no network request. None if not possible."""
    try:
        data_url = target.evaluate(_CANVAS_JS)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data_url, str) and "," in data_url:
        return base64.b64decode(data_url.split(",", 1)[1])
    return None


def _wait_prompt_box(page, css: str, deadline: float, busy_poll_s: float) -> None:  # type: ignore[no-untyped-def]
    while page.locator(css).count() == 0:
        assert_on_gemini(page.url)
        if time.time() > deadline:
            raise GeminiError("کادر پرامپت Gemini در گفتگوی تازه پیدا نشد. Chrome را بررسی کنید.")
        page.wait_for_timeout(int(busy_poll_s * 1000))


# Indices of images whose real size is loaded and at least min_side. Icons (sparkle, avatar) are skipped.
_BIG_IMAGES_JS = "(els, m) => els.map((e, i) => (e.complete && e.naturalWidth >= m && e.naturalHeight >= m) ? i : -1).filter(i => i >= 0)"


_LAST_RESPONSE_JS = ("() => { const r = document.querySelectorAll('model-response'); const last = r[r.length - 1];"
                     " if (!last) return {responses: 0}; return {responses: r.length,"
                     " imgs_in_last: last.querySelectorAll('img').length,"
                     " text_chars_in_last: (last.innerText || '').length}; }")


def _image_hint(page) -> str:  # type: ignore[no-untyped-def]
    """Numbers only (no chat text) to explain a timeout."""
    try:
        info = page.evaluate(_LAST_RESPONSE_JS)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(info, dict):
        return ""
    return (f" (پاسخ‌های Gemini: {info.get('responses')}، تصویر در آخرین پاسخ: {info.get('imgs_in_last')}،"
            f" طول متن آخرین پاسخ: {info.get('text_chars_in_last')} کاراکتر)")


def _big_image_indices(page, css: str, min_side: int) -> list[int]:  # type: ignore[no-untyped-def]
    return list(page.locator(css).evaluate_all(_BIG_IMAGES_JS, min_side))


def generate_image(prompt: str, out_dir: Path, timeout_s: int = 180, port: int = DEFAULT_DEBUG_PORT,
                   busy_poll_s: float = 1.0, new_chat: bool = False, min_side: int = 512) -> Path:
    """Type the prompt into Gemini, wait for a new image, save it raw (no crop/logo here).

    new_chat=True opens a fresh Gemini conversation first (the 'restart every N images' rule).
    Every step re-checks that the tab is still on gemini.google.com.
    """
    sel = load_selectors()
    require_verified(sel)
    pw, browser = _connect(port)
    try:
        page = _gemini_page(browser)
        assert_on_gemini(page.url)
        deadline = time.time() + timeout_s
        if new_chat:
            page.goto(GEMINI_URL)
            assert_on_gemini(page.url)
            _wait_prompt_box(page, sel.elements["prompt_box"], deadline, busy_poll_s)
        images = sel.elements["image_result"]
        before = len(_big_image_indices(page, images, min_side))

        box = page.locator(sel.elements["prompt_box"]).first
        box.click()
        box.fill(prompt)
        assert_on_gemini(page.url)
        page.locator(sel.elements["send_button"]).first.click()

        while len(_big_image_indices(page, images, min_side)) <= before:
            assert_on_gemini(page.url)
            if time.time() > deadline:
                raise GeminiError("Gemini در مهلت زمانی تصویری نساخت. دوباره تلاش کنید یا پرامپت را ساده‌تر کنید."
                                  + _image_hint(page))
            page.wait_for_timeout(int(busy_poll_s * 1000))

        busy = sel.elements.get("busy_indicator", "")
        while busy and page.locator(busy).count() > 0:
            assert_on_gemini(page.url)
            if time.time() > deadline:
                raise GeminiError("Gemini هنوز در حال تولید تصویر است؛ مهلت تمام شد.")
            page.wait_for_timeout(int(busy_poll_s * 1000))

        assert_on_gemini(page.url)
        big = _big_image_indices(page, images, min_side)
        target = page.locator(images).nth(big[-1])  # the newest large, fully loaded image
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"gemini_{int(time.time())}.png"
        src = target.get_attribute("src") or ""
        data = _canvas_bytes(target) if src.startswith("blob:") else None
        if not data:
            data = _image_bytes(page, src)
        if data:
            out.write_bytes(data)
        else:
            target.screenshot(path=str(out))
        log.info("gemini image saved: %s", out.name)
        return out
    finally:
        pw.stop()  # never browser.close(): it is the admin's own Chrome window


def require_text_ready(sel: Selectors) -> None:
    """Text writing needs the three image selectors and the response_text selector."""
    require_verified(sel)
    if not sel.elements.get("response_text"):
        raise GeminiError("سلکتور پاسخ Gemini هنوز تعریف نشده است. --inspect را وقتی یک پاسخ متنی روی صفحه است اجرا کنید.")


def _same_text(a: str, b: str) -> bool:
    """True when two texts are equal after collapsing whitespace (used to spot our own prompt)."""
    return " ".join(a.split()) == " ".join(b.split())


def generate_text(prompt: str, timeout_s: int = 180, port: int = DEFAULT_DEBUG_PORT,
                  busy_poll_s: float = 1.0, new_chat: bool = True) -> str:
    """Type the prompt into the Gemini tab and return the answer.

    new_chat=True (default): open a fresh conversation in the SAME tab first (no new tab is created).
    new_chat=False: use the conversation that is already open.
    """
    sel = load_selectors()
    require_text_ready(sel)
    pw, browser = _connect(port)
    try:
        page = _gemini_page(browser)
        assert_on_gemini(page.url)
        deadline = time.time() + timeout_s
        if new_chat:
            page.goto(GEMINI_URL)
            assert_on_gemini(page.url)
        _wait_prompt_box(page, sel.elements["prompt_box"], deadline, busy_poll_s)
        responses = sel.elements["response_text"]
        before = page.locator(responses).count()

        box = page.locator(sel.elements["prompt_box"]).first
        box.click()
        box.fill(prompt)
        typed = " ".join((box.inner_text() or "").split())
        if " ".join(prompt.split())[:20] not in typed:
            raise GeminiError("متن پرامپت در کادر Gemini نوشته نشد. دوباره /new را بزنید.")
        assert_on_gemini(page.url)
        page.locator(sel.elements["send_button"]).first.click()
        sent_by = time.time() + 5
        want = " ".join(prompt.split())[:20]
        while want in " ".join((box.inner_text() or "").split()):
            assert_on_gemini(page.url)
            if time.time() > sent_by:
                raise GeminiError("پیام به Gemini ارسال نشد. دکمه‌ی ارسال را بررسی کنید و دوباره /new را بزنید.")
            page.wait_for_timeout(300)

        while page.locator(responses).count() <= before:
            assert_on_gemini(page.url)
            if time.time() > deadline:
                raise GeminiError("Gemini در مهلت زمانی پاسخی نداد. دوباره امتحان کنید.")
            page.wait_for_timeout(int(busy_poll_s * 1000))

        busy = sel.elements.get("busy_indicator", "")
        while busy and page.locator(busy).count() > 0:
            assert_on_gemini(page.url)
            if time.time() > deadline:
                raise GeminiError("Gemini هنوز در حال نوشتن است؛ مهلت تمام شد.")
            page.wait_for_timeout(int(busy_poll_s * 1000))

        # the answer is final when two reads in a row are identical
        last, stable = "", 0
        while True:
            assert_on_gemini(page.url)
            text = (page.locator(responses).last.inner_text() or "").strip()
            if _same_text(text, prompt):
                text = ""  # the matched element is our own message, not Gemini's answer yet
            stable = stable + 1 if text and text == last else 0
            if stable >= 3:  # three identical reads in a row: the answer is complete
                return text
            last = text
            if time.time() > deadline:
                if text:
                    return text
                raise GeminiError("متن پاسخ Gemini خوانده نشد.")
            page.wait_for_timeout(int(busy_poll_s * 1000))
    finally:
        pw.stop()  # never browser.close()
