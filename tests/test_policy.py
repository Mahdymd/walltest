from decimal import Decimal

from app.composer import compose
from app.config import load_config
from app.policy import PolicyContext, PolicyInput, SourceResult, evaluate, find_banned
from app.sheets import COLUMNS


def ctx_for(cfg, source=None, recent=None):
    return PolicyContext(cfg=cfg, banned=[("کلاهبرداری", "hard"), ("بدون ریسک", "soft")],
                         allowed_domains={"example.com"}, source=source, recent=recent or [])


def make(cfg, title="", body="", fulltext="", source="", hashtags="#تست", image_mode="none", cta=""):
    fields = {"Title": title, "Body": body, "FullText": fulltext, "SourceURL": source,
              "Hashtags": hashtags, "CTA": cta, "ImageMode": image_mode}
    comp = compose(fields, cfg)
    inp = PolicyInput(post_id="W-0001", title=title, body=body, fulltext=fulltext, source_url=source,
                      hashtags=hashtags, cta=cta, image_mode=image_mode, image_style="", image_prompt="",
                      caption_html=comp.html, caption_plain=comp.plain, has_image=False)
    return inp


def cats(rep, level=None):
    return {f.category for f in rep.findings if level is None or f.level == level}


def test_clean_text_passes():
    cfg = load_config()
    rep = evaluate(make(cfg, title="سلام", body="متن ساده و بدون عدد."), ctx_for(cfg))
    assert rep.verdict == "PASS"


def test_hard_word_blocks_and_snippet_is_shown():
    cfg = load_config()
    rep = evaluate(make(cfg, body="این یک کلاهبرداری است."), ctx_for(cfg))
    assert rep.verdict == "BLOCK"
    assert "banned" in cats(rep, "BLOCK")
    assert any("کلاهبرداری" in f.snippet for f in rep.findings)


def test_spaced_word_still_caught():
    cfg = load_config()
    rep = evaluate(make(cfg, body="ک ل ا ه ب ر د ا ر ی"), ctx_for(cfg))
    assert rep.verdict == "BLOCK"


def test_soft_word_reviews():
    cfg = load_config()
    rep = evaluate(make(cfg, body="بدون ریسک"), ctx_for(cfg))
    assert rep.verdict == "REVIEW"


def test_number_without_source_blocks():
    cfg = load_config()
    rep = evaluate(make(cfg, body="رشد ۴۰٪ی بازار"), ctx_for(cfg))
    assert rep.verdict == "BLOCK"
    assert any(f.msg == "عدد بدون منبع" and f.snippet for f in rep.findings)


def test_number_found_in_source_passes():
    cfg = load_config()
    src = SourceResult(url="https://example.com/a", domain="example.com", ok=True,
                       text="The market grew 40 percent last year.")
    inp = make(cfg, body="رشد 40 درصدی", source="https://example.com/a")
    rep = evaluate(inp, ctx_for(cfg, source=src))
    assert "numbers" not in cats(rep)


def test_number_missing_in_source_reviews():
    cfg = load_config()
    src = SourceResult(url="https://example.com/a", domain="example.com", ok=True, text="nothing here 2026")
    rep = evaluate(make(cfg, body="۲۰ مورد", source="https://example.com/a"), ctx_for(cfg, source=src))
    assert any(f.level == "REVIEW" and f.category == "numbers" for f in rep.findings)


def test_twenty_does_not_match_2026_in_source():
    cfg = load_config()
    src = SourceResult(url="https://example.com/a", domain="example.com", ok=True, text="in 2026 things changed")
    rep = evaluate(make(cfg, body="۲۰ درصد", source="https://example.com/a"), ctx_for(cfg, source=src))
    assert any(f.category == "numbers" and f.level == "REVIEW" for f in rep.findings)


def test_price_not_found_blocks():
    cfg = load_config()
    src = SourceResult(url="https://example.com/a", domain="example.com", ok=True, text="price is 10 dollars")
    rep = evaluate(make(cfg, body="قیمت ۵۰۰ دلار", source="https://example.com/a"), ctx_for(cfg, source=src))
    assert rep.verdict == "BLOCK"


def test_unavailable_source_reviews_once():
    cfg = load_config()
    src = SourceResult(url="https://example.com/a", domain="example.com", ok=False, reason="HTTP 500")
    rep = evaluate(make(cfg, body="۱۰ و ۲۰ و ۳۰", source="https://example.com/a"), ctx_for(cfg, source=src))
    reviews = [f for f in rep.findings if f.category == "numbers"]
    assert len(reviews) == 1 and reviews[0].level == "REVIEW"


def test_length_over_limit_blocks():
    cfg = load_config()
    rep = evaluate(make(cfg, body="الف " * 1100), ctx_for(cfg))
    assert rep.verdict == "BLOCK"
    assert "length" in cats(rep, "BLOCK")


def test_non_whitelisted_domain_reviews():
    cfg = load_config()
    rep = evaluate(make(cfg, body="متن", source="https://unknown.org/x"), ctx_for(cfg))
    assert any(f.category == "source_url" and f.level == "REVIEW" for f in rep.findings)


def test_http_and_shortener_review_and_invalid_blocks():
    cfg = load_config()
    rep = evaluate(make(cfg, body="متن", source="http://example.com/x"), ctx_for(cfg))
    assert any(f.level == "REVIEW" and "http" in f.msg for f in rep.findings)
    rep2 = evaluate(make(cfg, body="متن", source="bit.ly/abc"), ctx_for(cfg))
    assert rep2.verdict == "BLOCK"  # invalid SourceURL


def test_duplicate_text_reviews():
    cfg = load_config()
    body = "این یک متن نسبتاً طولانی برای تست تکراری بودن است و باید کاملاً مشابه باشد."
    rep = evaluate(make(cfg, body=body), ctx_for(cfg, recent=[("W-0009", body)]))
    assert any(f.category == "duplicate" for f in rep.findings)


def test_hashtag_count_and_repeated_marks():
    cfg = load_config()
    rep = evaluate(make(cfg, body="خیلی عالی!!! واقعاً!!! ؟؟؟", hashtags=""), ctx_for(cfg))
    assert "hashtags" in cats(rep)
    assert "marks" in cats(rep)


def test_latin_only_reviews():
    cfg = load_config()
    rep = evaluate(make(cfg, body="this is a completely english caption for testing"), ctx_for(cfg))
    assert "latin" in cats(rep)


def test_mention_of_others_reviews():
    cfg = load_config()
    rep = evaluate(make(cfg, body="کانال @otherchannel را ببینید"), ctx_for(cfg))
    assert "links" in cats(rep)


def test_find_banned_normalised_and_squashed():
    hits = find_banned("ک-ل-م-ه‌ی کلاهبرداری", [("کلاهبرداری", "hard")])
    assert hits and hits[0][0] == "کلاهبرداری"


def test_invalid_image_mode_blocks():
    cfg = load_config()
    rep = evaluate(make(cfg, body="متن", image_mode="some-unknown-mode"), ctx_for(cfg))
    assert rep.verdict == "BLOCK"


def test_report_render_lists_pass_categories_and_roundtrips():
    cfg = load_config()
    rep = evaluate(make(cfg, body="سلام"), ctx_for(cfg))
    text = rep.render(version=2)
    assert "✅ فیلتر سیاسی" in text and "نسخه 2" in text
    from app.policy import Report
    again = Report.from_json(rep.to_json())
    assert again.verdict == rep.verdict and again.length == rep.length


_ = (COLUMNS, Decimal)
