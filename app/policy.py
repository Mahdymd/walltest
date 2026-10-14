"""Policy engine: PASS / REVIEW / BLOCK with exact snippets. Pure code, no AI."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from rapidfuzz import fuzz

from .config import Config
from .sheets import IMAGE_MODES, IMAGE_STYLES, clean
from .textutil import (
    extract_figures, figure_dates, figure_values, has_latin_letters, has_persian,
    normalize, squash, strip_html, utf16_len,
)

CATEGORIES = ["banned", "source_url", "numbers", "dates", "links", "length", "hashtags",
              "emojis", "marks", "lines", "latin", "duplicate", "image"]
CATEGORY_TITLES = {
    "banned": "فیلتر سیاسی", "source_url": "لینک منبع", "numbers": "عدد و منبع",
    "dates": "تاریخ", "links": "لینک‌ها و منشن‌ها", "length": "طول", "hashtags": "هشتگ‌ها",
    "emojis": "ایموجی", "marks": "علامت‌های تکراری", "lines": "طول خط‌ها",
    "latin": "زبان متن", "duplicate": "تکراری بودن", "image": "تصویر",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+")
HREF_RE = re.compile(r'href="([^"]+)"')
MENTION_RE = re.compile(r"@\w+")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF\u2600-\u27BF\u2B50\u2B55\u2190-\u21FF]")
FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")
MARKS_RE = re.compile(r"[!?؟]{2,}")
DOMAIN_OK_RE = re.compile(r"^https?://[^\s/]+\.[^\s/]+")


@dataclass
class Finding:
    level: str  # REVIEW | BLOCK
    category: str
    msg: str
    snippet: str = ""


@dataclass
class SourceResult:
    url: str
    domain: str
    ok: bool
    text: str = ""
    reason: str = ""


@dataclass
class PolicyInput:
    post_id: str
    title: str
    body: str
    fulltext: str
    source_url: str
    hashtags: str
    cta: str
    image_mode: str
    image_style: str
    image_prompt: str
    caption_html: str
    caption_plain: str
    has_image: bool = False


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    length: int = 0
    max_length: int = 0
    hashtags: int = 0
    emojis: int = 0
    watermark: str = "نامعلوم"

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "BLOCK"]

    @property
    def reviews(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "REVIEW"]

    @property
    def verdict(self) -> str:
        if self.blocks:
            return "BLOCK"
        if self.reviews:
            return "REVIEW"
        return "PASS"

    @property
    def summary(self) -> str:
        return f"⚠️{len(self.reviews)} ⛔{len(self.blocks)}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "Report":
        if not text:
            return Report()
        data = json.loads(text)
        data["findings"] = [Finding(**f) for f in data.get("findings", [])]
        return Report(**data)

    def render(self, version: int | None = None) -> str:
        """Plain Persian text. Caller must HTML-escape it before sending."""
        head = "📋 گزارش سیاست‌ها" + (f" — نسخه {version}" if version else "")
        lines = [head]
        cats_with_findings = {f.category for f in self.findings}
        for f in self.findings:
            icon = "⛔" if f.level == "BLOCK" else "⚠️"
            extra = f' «{f.snippet}»' if f.snippet else ""
            lines.append(f"{icon} {f.msg}{extra}")
        for cat in CATEGORIES:
            if cat not in cats_with_findings:
                lines.append(f"✅ {CATEGORY_TITLES[cat]}")
        if self.changes:
            lines.append("🔧 اصلاحات مکانیکی: " + "؛ ".join(self.changes))
        lines.append(f"📏 طول: {self.length}/{self.max_length}")
        lines.append(f"🏷 هشتگ: {self.hashtags} | 😀 ایموجی: {self.emojis}")
        lines.append(f"🖼 Media Watermark: {self.watermark}")
        for n in self.notes:
            lines.append(f"ℹ️ {n}")
        lines.append(f"نتیجه: {self.verdict} ({self.summary})")
        return "\n".join(lines)


@dataclass
class PolicyContext:
    cfg: Config
    banned: list[tuple[str, str]]
    allowed_domains: set[str]
    source: SourceResult | None = None
    recent: list[tuple[str, str]] = field(default_factory=list)
    watermark: str = "نامعلوم"  # from the /testimage probe (geministate.watermark_label)


def _domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _domain_allowed(domain: str, allowed: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def find_banned(text: str, banned: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(word, level, snippet) for each banned word found. Matches normalised and squashed text."""
    n_text = normalize(text)
    s_text = squash(text)
    hits: list[tuple[str, str, str]] = []
    for word, level in banned:
        n_word = normalize(word)
        if not n_word:
            continue
        pat = re.compile(r"(?<!\w)" + re.escape(n_word) + r"(?!\w)")
        m = pat.search(n_text)
        if m:
            a, b = max(0, m.start() - 25), min(len(n_text), m.end() + 25)
            hits.append((word, level, n_text[a:b]))
            continue
        s_word = squash(word)
        if s_word and len(s_word) >= 3:
            m2 = re.compile(r"(?<!\w)" + re.escape(s_word) + r"(?!\w)").search(s_text)
            if m2:
                a, b = max(0, m2.start() - 25), min(len(s_text), m2.end() + 25)
                hits.append((word, level, s_text[a:b]))
    return hits


def evaluate(inp: PolicyInput, ctx: PolicyContext) -> Report:
    cfg = ctx.cfg
    rep = Report()
    pol = cfg.policy
    handle = cfg.channel.handle

    # --- image mode / style
    mode = clean(inp.image_mode).lower() or "gemini"
    if mode not in IMAGE_MODES:
        rep.findings.append(Finding("BLOCK", "image", f"حالت تصویر نامعتبر است: «{inp.image_mode}» (gemini، none یا auto)"))
    style = clean(inp.image_style).lower()
    if style and style not in IMAGE_STYLES:
        rep.findings.append(Finding("REVIEW", "image", f"سبک تصویر نامعتبر است و «photo» استفاده می‌شود: «{style}»"))

    # --- source url
    src_url = clean(inp.source_url)
    src_domain = ""
    if src_url:
        if not DOMAIN_OK_RE.match(src_url):
            rep.findings.append(Finding("BLOCK", "source_url", "SourceURL نامعتبر است"))
        else:
            src_domain = _domain_of(src_url)
            if src_url.lower().startswith("http://"):
                rep.findings.append(Finding("REVIEW", "source_url", "لینک منبع http است، نه https", src_url))
            if any(src_domain == s or src_domain.endswith("." + s) for s in pol.shorteners):
                rep.findings.append(Finding("REVIEW", "source_url", "لینک کوتاه‌شده برای منبع مجاز نیست", src_url))
            if not _domain_allowed(src_domain, ctx.allowed_domains):
                rep.findings.append(Finding("REVIEW", "source_url", f"دامنه «{src_domain}» در فهرست Sources نیست", src_url))

    # --- banned words (caption + image prompt)
    text_for_banned = inp.caption_plain + "\n" + inp.image_prompt
    seen: set[str] = set()
    for word, level, snip in find_banned(text_for_banned, ctx.banned):
        if word in seen:
            continue
        seen.add(word)
        if level == "hard":
            rep.findings.append(Finding("BLOCK", "banned", f"کلمه‌ی ممنوع (hard): «{word}»", snip))
        else:
            rep.findings.append(Finding("REVIEW", "banned", f"کلمه‌ی حساس (soft): «{word}»", snip))

    # --- figures
    figs = extract_figures(inp.caption_plain)
    src = ctx.source
    src_ok = bool(src and src.ok)
    src_values = figure_values(extract_figures(src.text)) if src_ok else set()
    unavailable_reported = False
    done_raw: set[str] = set()
    for f in figs:
        if f.kind == "date" or f.raw in done_raw:
            continue
        done_raw.add(f.raw)
        is_money = f.kind == "currency"
        if not src_url:
            rep.findings.append(Finding("BLOCK", "numbers",
                                        "قیمت/واحد پولی بدون منبع" if is_money else "عدد بدون منبع", f.raw))
            continue
        if not src_domain or not DOMAIN_OK_RE.match(src_url):
            continue  # already BLOCKed as invalid SourceURL
        if not src_ok:
            if not unavailable_reported:
                reason = src.reason if src else "منبع بررسی نشد"
                rep.findings.append(Finding("REVIEW", "numbers", f"منبع قابل بررسی نبود ({reason})", src_url))
                unavailable_reported = True
            continue
        if f.value is None or f.value not in src_values:
            if is_money:
                rep.findings.append(Finding("BLOCK", "numbers", "قیمت در منبع پیدا نشد", f.raw))
            else:
                rep.findings.append(Finding("REVIEW", "numbers", "عدد در منبع پیدا نشد", f.raw))

    # --- dates (light)
    dates = [f for f in figs if f.kind == "date"]
    if dates:
        src_dates = figure_dates(extract_figures(src.text)) if src_ok else set()
        for f in dates:
            if f.date not in src_dates:
                rep.findings.append(Finding("REVIEW", "dates", "تاریخ را بررسی کنید", f.raw))

    # --- links and mentions
    allowed_urls = {src_url} if src_url else set()
    for href in HREF_RE.findall(inp.caption_html):
        _check_link(href, rep, allowed_urls, src_domain, ctx, pol)
    for url in URL_RE.findall(strip_html(inp.caption_html)):
        if url not in allowed_urls:
            _check_link(url, rep, allowed_urls, src_domain, ctx, pol)
    for mention in set(MENTION_RE.findall(inp.caption_plain)):
        if mention.lower() != handle.lower():
            rep.findings.append(Finding("REVIEW", "links", "منشن غیرمجاز (احتمال تبلیغ دیگران)", mention))

    # --- length
    rep.length = utf16_len(strip_html(inp.caption_html))
    rep.max_length = cfg.caption.max_chars_image if inp.has_image else cfg.caption.max_chars_text
    if rep.length > rep.max_length:
        rep.findings.append(Finding("BLOCK", "length", f"{rep.length - rep.max_length} کاراکتر اضافه (سقف {rep.max_length})"))

    # --- hashtags
    tags = [t for t in clean(inp.hashtags).replace(",", " ").split() if t.strip("#")]
    rep.hashtags = len(tags)
    if not (pol.hashtags_min <= rep.hashtags <= pol.hashtags_max):
        rep.findings.append(Finding("REVIEW", "hashtags",
                                    f"تعداد هشتگ {rep.hashtags} خارج از بازه‌ی {pol.hashtags_min} تا {pol.hashtags_max}"))

    # --- emojis, marks, lines, language
    rep.emojis = len(EMOJI_RE.findall(inp.caption_plain))
    if rep.emojis > pol.max_emojis:
        rep.findings.append(Finding("REVIEW", "emojis", f"{rep.emojis} ایموجی (بیش از {pol.max_emojis})"))
    if pol.flag_emojis_review and FLAG_RE.search(inp.caption_plain):
        rep.findings.append(Finding("REVIEW", "emojis", "ایموجی پرچم کشور وجود دارد"))
    marks = MARKS_RE.findall(inp.caption_plain)
    if len(marks) > pol.max_repeated_marks:
        rep.findings.append(Finding("REVIEW", "marks", f"{len(marks)} علامت تکراری (مثل «!!!»)", marks[0]))
    for line in inp.caption_plain.split("\n"):
        if len(line) > pol.max_line_chars:
            rep.findings.append(Finding("REVIEW", "lines", f"خطی با {len(line)} کاراکتر بدون شکست", line[:40] + "…"))
            break
    prose = re.sub(r"#\S+|@\w+|https?://\S+", " ", inp.caption_plain)  # hashtags/handles don't count
    if pol.latin_only_review and not has_persian(prose) and has_latin_letters(prose) >= 10:
        rep.findings.append(Finding("REVIEW", "latin", "متن تماماً لاتین است"))

    # --- duplicates
    norm_now = normalize(inp.caption_plain)
    for pid, other in ctx.recent:
        if not other:
            continue
        ratio = fuzz.ratio(norm_now, normalize(other)) / 100
        if ratio >= pol.duplicate_threshold:
            rep.findings.append(Finding("REVIEW", "duplicate", f"مشابه {pid} ({int(ratio * 100)}٪)"))
            break

    # --- watermark: result of the /testimage probe (the admin answers yes/no once)
    rep.watermark = ctx.watermark
    return rep


def _check_link(url: str, rep: Report, allowed_urls: set[str], src_domain: str,
                ctx: PolicyContext, pol) -> None:  # noqa: ANN001
    if url in allowed_urls:
        return
    domain = _domain_of(url)
    if url.lower().startswith("http://"):
        rep.findings.append(Finding("REVIEW", "links", "لینک http (نه https)", url))
        return
    if any(domain == s or domain.endswith("." + s) for s in pol.shorteners):
        rep.findings.append(Finding("REVIEW", "links", "لینک کوتاه‌شده", url))
        return
    if src_domain and (domain == src_domain or domain.endswith("." + src_domain)):
        return
    if domain in ("t.me", "telegram.me") and ctx.cfg.channel.handle.lstrip("@").lower() in url.lower():
        return
    rep.findings.append(Finding("REVIEW", "links", "لینک غیرمجاز (احتمال تبلیغ دیگران)", url))
