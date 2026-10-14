"""Look for an existing, freely licensed photo on Wikimedia Commons before generating one.

Only files whose license allows reuse with credit are accepted: CC0, Public Domain and CC BY / CC BY-SA.
NC (non-commercial) and ND (no derivatives) licenses are refused, because the bot always crops images.
Every accepted file keeps its file page URL, which becomes the post's SourceURL (credit + link).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from .textutil import strip_html

log = logging.getLogger(__name__)
USER_AGENT = "WalAgent/2.0 (Telegram channel bot; contact the channel admin)"
ALLOWED_MIME = ("image/jpeg", "image/png")
PAGE_PREFIX = "https://commons.wikimedia.org/"
FILE_PREFIX = "https://upload.wikimedia.org/"


class CommonsError(Exception):
    """Download problem. Never shown as a hard failure: the caller falls back to generation."""


@dataclass(frozen=True)
class Hit:
    page_url: str
    image_url: str
    width: int
    height: int
    license: str
    author: str


def license_allowed(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    if re.search(r"-nc\b|-nd\b", n):  # NC and ND variants are refused
        return False
    return n.startswith(("cc0", "cc by", "public domain"))


def _client(proxy: str = "", transport=None) -> httpx.Client:  # type: ignore[no-untyped-def]
    kwargs: dict = {"timeout": 15, "headers": {"User-Agent": USER_AGENT}, "follow_redirects": True}
    if transport is not None:
        kwargs["transport"] = transport
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def search(terms: str, *, api: str, min_side: int, max_results: int, proxy: str = "",
           transport=None) -> Hit | None:  # type: ignore[no-untyped-def]
    query = " ".join((terms or "").split())[:120]
    if not query:
        return None
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": str(max_results),
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
    }
    with _client(proxy, transport) as client:
        resp = client.get(api, params=params)
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or []
    pages.sort(key=lambda p: p.get("index", 0))
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ALLOWED_MIME:
            continue
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if min(width, height) < min_side:
            continue
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName") or {}).get("value", "")
        if not license_allowed(license_name):
            continue
        page_url, image_url = info.get("descriptionurl") or "", info.get("url") or ""
        if not (page_url.startswith(PAGE_PREFIX) and image_url.startswith(FILE_PREFIX)):
            continue
        author = strip_html((meta.get("Artist") or {}).get("value", "")) or "نامشخص"
        return Hit(page_url=page_url, image_url=image_url, width=width, height=height,
                   license=license_name, author=author[:120])
    return None


def download(hit: Hit, dest: Path, *, max_bytes: int, proxy: str = "", transport=None) -> Path:  # type: ignore[no-untyped-def]
    buf = bytearray()
    with _client(proxy, transport) as client:
        with client.stream("GET", hit.image_url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise CommonsError("فایل عکس از حد مجاز بزرگ‌تر است")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(buf))
    return dest


def find_photo(terms: str, dest_dir: Path, *, api: str, min_side: int, max_results: int,
               max_bytes: int, proxy: str = "") -> tuple[Hit, Path] | None:
    """Blocking. Returns (hit, local file) or None. Network errors are logged and mean 'nothing found'."""
    try:
        hit = search(terms, api=api, min_side=min_side, max_results=max_results, proxy=proxy)
        if hit is None:
            return None
        ext = ".png" if hit.image_url.lower().endswith(".png") else ".jpg"
        path = download(hit, dest_dir / f"commons{ext}", max_bytes=max_bytes, proxy=proxy)
        return hit, path
    except (httpx.HTTPError, CommonsError, ValueError) as exc:
        log.warning("commons search failed: %s", str(exc)[:200])
        return None
