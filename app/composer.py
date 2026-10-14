"""Deterministic caption assembly. No AI, no translation, no rewriting."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Config
from .sheets import clean
from .textutil import balanced_html, mechanical_fixes, parse_hashtags, sanitize_allowed_html, strip_html

INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore (all )?(the )?(previous|above) (instructions|prompts?)"),
    re.compile(r"(?i)disregard (the )?(previous|above)"),
    re.compile(r"(?i)system prompt"),
    re.compile(r"(?i)you are now"),
    re.compile(r"ignore\s+قبلی"),
    re.compile(r"دستورات? (قبلی|بالا) را نادیده"),
]
_URL_IN_TEXT = re.compile(r"https?://\S+|www\.\S+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029]")


@dataclass
class Composed:
    html: str
    plain: str
    mode: str  # fulltext | template
    changes: list[str]
    notes: list[str]


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def compose(fields: dict[str, str], cfg: Config) -> Composed:
    changes: list[str] = []
    notes: list[str] = []
    handle = cfg.channel.handle
    fulltext = clean(fields.get("FullText", ""))
    source_url = clean(fields.get("SourceURL", ""))
    parts: list[str] = []

    if fulltext:
        mode = "fulltext"
        body = sanitize_allowed_html(fulltext)
        body, ch = mechanical_fixes(body)
        changes += ch
        parts.append(body)
        if clean(fields.get("Hashtags", "")) or clean(fields.get("CTA", "")):
            notes.append("در حالت FullText، ستون‌های Hashtags و CTA به کپشن اضافه نمی‌شوند")
    else:
        mode = "template"
        title = clean(fields.get("Title", ""))
        body = "\n".join(ln for ln in clean(fields.get("Body", "")).split("\n") if ln.strip() != handle)
        body = body.strip()
        cta = clean(fields.get("CTA", ""))
        if title:
            t, ch = mechanical_fixes(_esc(title))
            changes += ch
            parts.append(cfg.caption.title_format.format(title=t))
        if body:
            b, ch = mechanical_fixes(_esc(body))
            changes += ch
            parts.append(b)
        if source_url:
            label = _domain(source_url) or source_url
            parts.append(cfg.caption.source_label_format.format(
                url=html.escape(source_url, quote=True), label=_esc(label)))
        tags, ch = parse_hashtags(clean(fields.get("Hashtags", "")))
        changes += ch
        if tags:
            parts.append(" ".join(tags))
        if cta:
            c, ch = mechanical_fixes(_esc(cta))
            changes += ch
            parts.append(c)

    text = "\n\n".join(p for p in parts if p.strip())
    if not text.strip():
        return Composed(html="", plain="", mode=mode, changes=changes, notes=notes)

    lines = [ln.rstrip() for ln in text.split("\n")]
    if not lines or lines[-1].strip() != handle:
        text = text.rstrip() + "\n\n" + handle
        changes.append(f"هندل {handle} به انتهای کپشن اضافه شد")
    final_html = text
    plain = html.unescape(strip_html(final_html)).strip()
    if not balanced_html(final_html):
        final_html = _esc(html.unescape(strip_html(final_html)))
        plain = html.unescape(strip_html(final_html)).strip()
        notes.append("تگ‌های HTML ناسازگار بودند و به متن ساده تبدیل شدند")
    return Composed(html=final_html, plain=plain, mode=mode, changes=changes, notes=notes)


def sanitize_prompt_input(text: str, max_chars: int) -> tuple[str, list[str]]:
    """Clean text that will be typed into an image prompt (phase 2). Returns (clean, removed_notes)."""
    notes: list[str] = []
    t = _CONTROL.sub(" ", text or "")
    t2 = _URL_IN_TEXT.sub(" ", t)
    if t2 != t:
        notes.append("URL از متن پرامپت حذف شد")
    t = t2
    for pat in INJECTION_PATTERNS:
        t2 = pat.sub(" ", t)
        if t2 != t:
            notes.append("عبارت دستوری از متن پرامپت حذف شد")
        t = t2
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip()
        notes.append(f"متن به {max_chars} کاراکتر کوتاه شد")
    return t, notes


def build_image_prompt(title: str, style: str, custom_prompt: str, cfg: Config) -> tuple[str, list[str]]:
    """Template prompt (or the sheet's ImagePrompt column if given). No AI involved."""
    ip = cfg.image_prompt
    notes: list[str] = []
    custom, n1 = sanitize_prompt_input(custom_prompt, 1000)
    notes += n1
    if custom:
        return custom, notes
    t, n2 = sanitize_prompt_input(title, ip.max_title_chars)
    notes += n2
    fragment = ip.style_fragments.get(style or "photo", ip.style_fragments.get("photo", ""))
    prompt = ip.template.format(style_fragment=fragment, title=t, brand_colors=ip.brand_colors)
    return prompt, notes
