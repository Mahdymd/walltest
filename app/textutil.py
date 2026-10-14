"""Persian text helpers: normalisation, UTF-16 length, figures (numbers), mechanical fixes.

Nothing in this module changes meaning. No AI, no ML.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- normalisation
_DIGITS = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGITS.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})
_ARABIC_TO_PERSIAN = {ord("ي"): "ی", ord("ك"): "ک", ord("ى"): "ی"}
_SPACED_LETTERS = re.compile(r"(?<!\S)(?:\S ){2,}\S(?!\S)")
_PUNCT_BETWEEN_WORDS = re.compile(r"(?<=\w)[\W_]+(?=\w)")


def to_ascii_digits(text: str) -> str:
    return text.translate(_DIGITS)


def normalize(text: str) -> str:
    """Normalisation used ONLY for comparison (policy). Never applied to output text."""
    t = unicodedata.normalize("NFKC", text)
    t = to_ascii_digits(t).translate(_ARABIC_TO_PERSIAN)
    # remove harakat (Mn), format chars such as ZWNJ/ZWJ (Cf) and tatweel
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Mn", "Cf") and ch != "\u0640")
    t = t.lower()
    # "ک ل م ه" -> "کلمه" (spaced-out letters used to dodge filters)
    t = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), t)
    return re.sub(r"\s+", " ", t).strip()


def squash(text: str) -> str:
    """Normalised text with punctuation between word characters removed: «ک-ل-م-ه» -> «کلمه»."""
    return _PUNCT_BETWEEN_WORDS.sub("", normalize(text))


def utf16_len(text: str) -> int:
    """Telegram counts entities in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


def has_persian(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))


def has_latin_letters(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", text))


# ---------------------------------------------------------------- mechanical fixes
_ZWNJ_RE = re.compile(r"[\u200c\u200d]")


def mechanical_fixes(text: str) -> tuple[str, list[str]]:
    """Safe, word-preserving clean-up. Returns (new_text, list_of_changes_in_persian)."""
    changes: list[str] = []
    t = text
    t2 = t.replace("ي", "ی").replace("ك", "ک")
    if t2 != t:
        changes.append("ي/ك عربی به ی/ک فارسی تبدیل شد")
        t = t2
    t2 = re.sub(r"[ \t\u00a0]{2,}", " ", t)
    if t2 != t:
        changes.append("فاصله‌ی تکراری حذف شد")
        t = t2
    lines = [ln.strip() for ln in t.split("\n")]
    t2 = "\n".join(lines)
    if t2 != t:
        changes.append("فاصله‌ی ابتدا/انتهای خطوط حذف شد")
        t = t2
    t2 = re.sub(r"\n{3,}", "\n\n", t)
    if t2 != t:
        changes.append("خط خالی اضافه حذف شد")
        t = t2
    t2 = t.strip()
    if t2 != t:
        changes.append("trim ابتدا/انتهای متن")
        t = t2
    return t, changes


_ALLOWED_TAG_RE = re.compile(
    r"&lt;(/?)(b|i|u|s|code)&gt;"
    r'|&lt;a href="(https?://[^"\s<>]+)"&gt;'
    r"|&lt;/a&gt;"
)


def sanitize_allowed_html(raw: str) -> str:
    """Escape everything, then restore only the tags Telegram accepts (b,i,u,s,code,a[href])."""
    escaped = html.escape(raw, quote=False)

    def restore(m: re.Match[str]) -> str:
        if m.group(0) == "&lt;/a&gt;":
            return "</a>"
        if m.group(3):
            return f'<a href="{html.escape(html.unescape(m.group(3)), quote=True)}">'
        return f"<{m.group(1)}{m.group(2)}>"

    return _ALLOWED_TAG_RE.sub(restore, escaped)


def balanced_html(text: str) -> bool:
    stack: list[str] = []
    for m in re.finditer(r"<(/?)(b|i|u|s|code|a)\b[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack


_HASHTAG_SPLIT = re.compile(r"[\s,،]+")


def parse_hashtags(raw: str) -> tuple[list[str], list[str]]:
    """Return (['#tag', ...], changes). Duplicates removed, '#' ensured, ZWNJ removed."""
    changes: list[str] = []
    tags: list[str] = []
    for tok in _HASHTAG_SPLIT.split(raw or ""):
        if not tok:
            continue
        body = tok.lstrip("#")
        clean = _ZWNJ_RE.sub("", body).replace(" ", "_")
        if clean != body:
            changes.append(f"اصلاح هشتگ «{tok}»")
        if not clean:
            continue
        tag = "#" + clean
        if tag not in tags:
            tags.append(tag)
        else:
            changes.append(f"هشتگ تکراری «{tag}» حذف شد")
    return tags, changes


# ---------------------------------------------------------------- figures (numbers, prices, percents, dates)
@dataclass(frozen=True)
class Figure:
    kind: str  # plain | percent | currency | date
    raw: str
    value: Decimal | None = None
    date: tuple[int, int, int] | None = None


_URL_RE = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|ir|org|net|io|me|co|info|app)(?:/\S*)?", re.I)
_HANDLE_RE = re.compile(r"@\w+")
_HASHTAG_RE = re.compile(r"#\S+")
_TECH_PATTERNS = [
    re.compile(r"\b\d{2,5}\s*[x×*]\s*\d{2,5}\b", re.I),  # resolutions 1280x720
    re.compile(r"\b(?:1080p|720p|1440p|2160p|4K|8K|2K)\b", re.I),
    re.compile(r"\b(?:[A-Z][A-Za-z]*|[a-z]+[A-Z][A-Za-z]*)[-\s]?v?\d+(?:\.\d+)*[A-Za-z]?\b"),  # GPT-5, iPhone 17, Gemini 3.1
    re.compile(r"\bv\d+(?:\.\d+)+\b", re.I),
]
_DATE_NUM = re.compile(r"(?<!\d)(1[34]\d{2}|20\d{2})[/\-.](0?[1-9]|1[0-2])[/\-.](0?[1-9]|[12]\d|3[01])(?!\d)")
_FA_MONTHS = {
    "فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5, "شهریور": 6,
    "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10, "بهمن": 11, "اسفند": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_DATE_NAMED = re.compile(
    r"(?<!\d)(\d{1,2})\s+(" + "|".join(list(_FA_MONTHS) + list(_EN_MONTHS)) + r")\s+(1[34]\d{2}|20\d{2})(?!\d)",
    re.I,
)
_SCALES = {
    "هزار": 1_000, "thousand": 1_000, "k": 1_000,
    "میلیون": 1_000_000, "million": 1_000_000, "m": 1_000_000,
    "میلیارد": 1_000_000_000, "billion": 1_000_000_000, "b": 1_000_000_000,
    "تریلیون": 1_000_000_000_000, "trillion": 1_000_000_000_000,
}
_CURRENCY_UNITS = {"تومان", "ریال", "دلار", "یورو", "usd", "eur", "irr", "irt", "$", "€", "£"}
_NUM_RE = re.compile(
    r"(?P<pre>[$€£]\s?)?"
    r"(?<![A-Za-z])"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>تریلیون|میلیارد|میلیون|هزار|trillion|billion|million|thousand|[KMB](?![A-Za-z])))?"
    r"(?:\s?(?P<unit>%|٪|درصد|تومان|ریال|دلار|یورو|USD|EUR|IRR|IRT|\$|€|£))?",
    re.I,
)

_WORD_NUMS = {
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7, "هشت": 8, "نه": 9,
    "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19, "بیست": 20, "سی": 30, "چهل": 40,
    "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90, "صد": 100,
    "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500, "ششصد": 600, "هفتصد": 700,
    "هشتصد": 800, "نهصد": 900, "هزار": 1000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000,
}
# Word numbers are only counted when followed by a unit, to avoid counting the article «یک».
_WORD_UNITS = {
    "برابر", "درصد", "درصدی", "تومان", "ریال", "دلار", "یورو", "نفر", "مرحله", "روز", "ساعت",
    "ماه", "سال", "بار", "مورد", "گام", "دقیقه", "ثانیه", "کاربر", "مشتری", "کیلومتر",
    "کیلوگرم", "گرم", "ساله", "هفته", "میلیون", "هزار", "میلیارد", "لیتر", "نوع", "راه",
}
_WORD_TOKEN = re.compile(r"[\u0600-\u06FF]+")


def _to_decimal(num: str) -> Decimal | None:
    try:
        return Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None


def _parse_word_run(words: list[str]) -> int:
    total = 0
    current = 0
    for w in words:
        if w == "و":
            continue
        v = _WORD_NUMS[w]
        if v >= 1000:
            total += (current or 1) * v
            current = 0
        elif v == 100:
            current = (current or 1) * 100
        else:
            current += v
    return total + current


_MONTH_WORDS = set(_EN_MONTHS)


def _tech_repl(m: re.Match[str]) -> str:
    word = m.group(0).split()[0].split("-")[0].lower()
    if word in _MONTH_WORDS:
        return m.group(0)
    if re.search(r"(?<!\d)(1[34]\d{2}|20\d{2})(?!\d)", m.group(0)):
        return m.group(0)  # years are real figures, not model numbers
    return " "


def _strip_noise(text: str) -> str:
    t = _URL_RE.sub(" ", text)
    t = _HANDLE_RE.sub(" ", t)
    t = _HASHTAG_RE.sub(" ", t)
    for p in _TECH_PATTERNS:
        t = p.sub(_tech_repl, t)
    return t


def extract_figures(text: str) -> list[Figure]:
    """Find numbers that matter for sourcing: digits (Persian/English), words, scales, %, currency, dates."""
    t = to_ascii_digits(text).replace("٫", ".").replace("٬", ",")
    t = re.sub(r"(?<=\d)،(?=\d{3})", ",", t)
    t = _strip_noise(t)
    figures: list[Figure] = []

    for m in _DATE_NUM.finditer(t):
        figures.append(Figure("date", m.group(0), date=(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    t = _DATE_NUM.sub(" ", t)
    for m in _DATE_NAMED.finditer(t):
        month_word = m.group(2).lower()
        month = _FA_MONTHS.get(m.group(2), _EN_MONTHS.get(month_word))
        figures.append(Figure("date", m.group(0), date=(int(m.group(3)), int(month or 0), int(m.group(1)))))
    t = _DATE_NAMED.sub(" ", t)

    spans: list[tuple[int, int]] = []
    for m in _NUM_RE.finditer(t):
        spans.append(m.span())
        num = _to_decimal(m.group("num"))
        if num is None:
            continue
        scale = m.group("scale")
        mult = _SCALES.get(scale.lower(), 1) if scale else 1
        unit = (m.group("unit") or "").lower()
        pre = (m.group("pre") or "").strip()
        if unit in ("%", "٪", "درصد"):
            kind = "percent"
        elif unit in _CURRENCY_UNITS or pre in _CURRENCY_UNITS:
            kind = "currency"
        else:
            kind = "plain"
        raw = m.group(0).strip()
        figures.append(Figure(kind, raw, value=num * mult))

    # Persian word numbers followed by a unit word, e.g. «دو برابر», «صد هزار تومان»
    blanked = list(t)
    for a, b in spans:
        for k in range(a, b):
            blanked[k] = " "
    t_words = "".join(blanked)
    tokens = [(m.group(0), m.start()) for m in _WORD_TOKEN.finditer(t_words)]
    i = 0
    while i < len(tokens):
        word = tokens[i][0]
        if word in _WORD_NUMS:
            j = i
            run = [word]
            while j + 1 < len(tokens):
                nxt_tok = tokens[j + 1][0]
                if nxt_tok in _WORD_NUMS:
                    run.append(nxt_tok)
                    j += 1
                elif nxt_tok == "و" and j + 2 < len(tokens) and tokens[j + 2][0] in _WORD_NUMS:
                    run += ["و", tokens[j + 2][0]]
                    j += 2
                else:
                    break
            nxt = tokens[j + 1][0] if j + 1 < len(tokens) else ""
            if nxt in _WORD_UNITS:
                value = Decimal(_parse_word_run(run))
                kind = "percent" if nxt in ("درصد", "درصدی") else ("currency" if nxt in _CURRENCY_UNITS or nxt in ("تومان", "ریال", "دلار", "یورو") else "plain")
                figures.append(Figure(kind, " ".join(run + [nxt]), value=value))
            i = j + 1
        else:
            i += 1
    return figures


def figure_values(figures: list[Figure]) -> set[Decimal]:
    return {f.value for f in figures if f.value is not None}


def figure_dates(figures: list[Figure]) -> set[tuple[int, int, int]]:
    return {f.date for f in figures if f.date is not None}


def find_outside_figures_text(text: str) -> list[str]:
    """Raw snippets of figures for report display."""
    return [f.raw for f in extract_figures(text) if f.kind != "date"]
