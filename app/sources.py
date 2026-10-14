"""Source page fetching: httpx first, headless Playwright Chromium as fallback for JS pages.

Runs blocking I/O; call with asyncio.to_thread. This browser is separate from the Gemini profile.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .policy import SourceResult

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; WalSourceReader/1.0; +https://t.me/waltest0001)"
MAX_CHARS = 300_000


def extract_text(markup: str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())[:MAX_CHARS]


def _render_with_chromium(url: str, timeout_s: int) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=UA)
                page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                text = page.inner_text("body", timeout=5000)
                return " ".join(text.split())[:MAX_CHARS]
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        log.info("chromium render failed: %s", str(exc)[:200])
        return ""


def fetch_source(url: str, allowed: set[str], timeout_s: int, min_chars: int,
                 proxy: str = "") -> SourceResult:
    host = urlparse(url).netloc.lower()
    domain = host[4:] if host.startswith("www.") else host
    if not any(domain == a or domain.endswith("." + a) for a in allowed):
        return SourceResult(url=url, domain=domain, ok=False, reason="دامنه در فهرست Sources نیست")
    text = ""
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True, headers={"User-Agent": UA},
                          proxy=proxy or None) as client:
            resp = client.get(url)
        if resp.status_code >= 400:
            return SourceResult(url=url, domain=domain, ok=False, reason=f"HTTP {resp.status_code}")
        text = extract_text(resp.text)
    except Exception as exc:  # noqa: BLE001
        log.info("source fetch failed for %s: %s", domain, str(exc)[:150])
        text = ""
    if len(text) < min_chars:
        rendered = _render_with_chromium(url, timeout_s)
        if len(rendered) > len(text):
            text = rendered
    if len(text) < min_chars:
        return SourceResult(url=url, domain=domain, ok=False, reason="متن صفحه قابل خواندن نبود")
    return SourceResult(url=url, domain=domain, ok=True, text=text)
