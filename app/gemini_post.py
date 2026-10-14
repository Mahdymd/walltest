"""Post text written by Gemini (same browser as phase 2) from a short brief in Telegram.

Gemini must answer with five labelled lines. Anything else is refused with a Persian message,
so a bad answer never reaches the sheet.
"""
from __future__ import annotations

import re

from . import gemini, geministate

PROMPT = (
    "لطفاً یک پست کوتاه و ساده به زبان فارسی برای یک کانال تلگرامی بنویس.\n"
    "موضوع پست: {brief}\n\n"
    "جواب را با این ساختار بنویس:\n"
    "TITLE: یک عنوان کوتاه\n"
    "BODY: متن پست در ۲ تا ۳ جمله\n"
    "HASHTAGS: ۲ تا ۴ هشتگ فارسی\n"
    "CTA: یک جمله‌ی دعوت به اقدام\n"
    "KEYWORDS: ۳ کلمه‌ی انگلیسی برای جستجوی عکس\n\n"
    "عدد، قیمت و تاریخ ننویس. ادعای قطعی نکن."
)
_LABEL_LINE = re.compile(r"^[\s*_#>\-•\d.)]*(?P<label>[^:：]{2,30}?)[\s*_]*[:：][\s*_]*(?P<value>.*)$")
_EXACT = {"title": "TITLE", "body": "BODY", "hashtags": "HASHTAGS", "cta": "CTA", "keywords": "KEYWORDS",
          "عنوان": "TITLE", "متن": "BODY", "هشتگ": "HASHTAGS", "هشتگ‌ها": "HASHTAGS", "هشتگ ها": "HASHTAGS",
          "دعوت به اقدام": "CTA", "کلیدواژه": "KEYWORDS", "کلیدواژه‌ها": "KEYWORDS", "کلمات کلیدی": "KEYWORDS"}
_PREFIX = (("هشتگ", "HASHTAGS"), ("کلیدواژ", "KEYWORDS"), ("کلمات کلیدی", "KEYWORDS"), ("دعوت", "CTA"))


def _key(label: str) -> str | None:
    norm = re.sub(r"[\s*_#]+", " ", label).strip().lower()
    if norm in _EXACT:
        return _EXACT[norm]
    for prefix, key in _PREFIX:
        if norm.startswith(prefix):
            return key
    return None


def _clean(value: str) -> str:
    return re.sub(r"[*_`#]+", "", value).strip()


_REFUSAL = re.compile(r"نمی[\u200c\s]?توان|نمی[\u200c\s]?تون|مدل زبان")


class PostError(Exception):
    """Message is Persian and shown to the admin as-is."""


PROMPT_PLAIN = (
    "یک متن کوتاه و دوستانه به زبان فارسی درباره‌ی «{brief}» برای یک کانال تلگرامی بنویس. "
    "دو یا سه جمله باشد و در آخر دو هشتگ فارسی بگذار. عدد و قیمت ننویس."
)


def build_prompt(brief: str, variant: str = "structured") -> str:
    template = PROMPT_PLAIN if variant == "plain" else PROMPT
    return template.replace("{brief}", " ".join(brief.split())[:500])


def check_ready(ctx) -> None:  # noqa: ANN001
    """Gate only (no budget used). Used before the admin is told that writing has started."""
    try:
        gemini.require_text_ready(gemini.load_selectors())
    except gemini.GeminiError as exc:
        raise PostError(str(exc)) from exc


def spend_text_budget(ctx) -> None:  # noqa: ANN001
    if not geministate.begin_text_attempt(ctx.store, ctx.cfg.gemini.max_text_per_day):
        raise PostError(f"سقف روزانه‌ی نوشتن متن با Gemini ({ctx.cfg.gemini.max_text_per_day} درخواست) پر شده است. فردا دوباره امتحان کنید.")


def write_post_blocking(brief: str, timeout_s: int, variant: str = "structured") -> str:
    """Worker thread. Opens a new Gemini chat, returns the raw answer text."""
    try:
        return gemini.generate_text(build_prompt(brief, variant), timeout_s=timeout_s, new_chat=True)
    except gemini.GeminiError as exc:
        raise PostError(str(exc)) from exc


def parse_post(text: str) -> dict[str, str]:
    """Accepts labels in English or Persian, with or without markdown, and multi-line values."""
    collected: dict[str, list[str]] = {}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LABEL_LINE.match(line)
        key = _key(m.group("label")) if m else None
        if key:
            current = key
            collected.setdefault(key, [])
            value = _clean(m.group("value"))
            if value:
                collected[key].append(value)
        elif current:
            collected[current].append(_clean(line))
    found = {k: " ".join(v for v in vals if v).strip() for k, vals in collected.items()}
    snippet = " ".join((text or "").split())[:200] or "(خالی)"
    if not found:
        # free text: no labels at all. Refusals are rejected; anything else becomes the post body.
        raw = " ".join((text or "").split())
        tags_in_text = re.findall(r"#\S+", raw)  # read hashtags before _clean removes the '#' marks
        plain = _clean(re.sub(r"#\S+", " ", raw))
        if not plain:
            raise PostError("Gemini جوابی نداد. دوباره /new را بزنید.")
        if _REFUSAL.search(plain):
            raise PostError(f"Gemini درخواست را رد کرد. ابتدای پاسخ: «{snippet}». "
                            "موضوع را واضح‌تر بنویسید و دوباره /new را بزنید.")
        found = {"TITLE": plain[:60].rstrip(" ،,.:") or "پست جدید", "BODY": plain[:1200],
                 "HASHTAGS": " ".join(tags_in_text)}
    if not found.get("TITLE") or not found.get("BODY"):
        raise PostError("پاسخ Gemini قالب لازم را نداشت (TITLE و BODY). "
                        f"ابتدای پاسخ: «{snippet}». دوباره /new را بزنید با موضوع واضح‌تر.")
    tags: list[str] = []
    for token in re.split(r"[\s,،]+", found.get("HASHTAGS", "")):
        word = token.strip("#").strip()
        if word:
            tags.append("#" + word)
    hashtags = " ".join(dict.fromkeys(tags[:6])) or "#وال"
    keywords = re.sub(r"[^A-Za-z0-9 \-]", " ", found.get("KEYWORDS", "")).split()
    return {
        "TITLE": found["TITLE"][:120],
        "BODY": found["BODY"][:1200],
        "HASHTAGS": hashtags,
        "CTA": found.get("CTA", "")[:200],
        "KEYWORDS": " ".join(keywords)[:120],
    }
