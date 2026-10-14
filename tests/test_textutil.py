from decimal import Decimal

from app.textutil import (balanced_html, extract_figures, figure_values, mechanical_fixes, normalize,
                          parse_hashtags, sanitize_allowed_html, squash, strip_html, utf16_len)


def vals(text):
    return figure_values(extract_figures(text))


def test_normalize_removes_zwnj_harakat_and_spaced_letters():
    assert normalize("ک ل م ه") == "کلمه"
    assert normalize("ک\u200cل\u200cم\u200cه") == "کلمه"
    assert normalize("كَلِمَه") == "کلمه"


def test_normalize_converts_digits():
    assert "2026" in normalize("۲۰۲۶")
    assert "۲" not in normalize("۲۰۲۶")


def test_squash_catches_separators():
    assert squash("ک-ل-م-ه") == "کلمه"


def test_utf16_length_counts_emoji_as_two():
    assert utf16_len("a") == 1
    assert utf16_len("😀") == 2
    assert utf16_len("سلام😀") == 6


def test_strip_html_and_balance():
    assert strip_html("<b>سلام</b> &amp; خوش") == "سلام & خوش"
    assert balanced_html("<b>x</b>")
    assert not balanced_html("<b>x")


def test_sanitize_keeps_only_allowed_tags():
    out = sanitize_allowed_html('a < b <b>سلام</b> <script>x</script>')
    assert "<b>سلام</b>" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_sanitize_link_with_ampersand_not_double_escaped():
    out = sanitize_allowed_html('<a href="https://x.ir/?a=1&b=2">لینک</a>')
    assert 'href="https://x.ir/?a=1&amp;b=2"' in out


def test_mechanical_fixes_report_changes():
    t, changes = mechanical_fixes("سلام   دنیا\n\n\n\nك")
    assert t == "سلام دنیا\n\nک"
    assert changes


def test_hashtags_parse_and_dedupe():
    tags, changes = parse_hashtags("#ارز_دیجیتال ، #ارز_دیجیتال  تست")
    assert tags == ["#ارز_دیجیتال", "#تست"]
    assert changes


# ---- figures (numbers)
def test_persian_digits_percent():
    assert vals("رشد ۴۰٪ی") == {Decimal(40)}


def test_scale_words_equal_numeric_forms():
    assert vals("۱٫۵ میلیارد") == vals("1.5 billion") == vals("1,500,000,000")


def test_20_does_not_match_2026():
    assert vals("2026") == {Decimal(2026)}
    assert Decimal(20) not in vals("2026")


def test_tech_names_are_ignored():
    assert vals("GPT-5 و iPhone 17 و Gemini 3.1 و 1280x720 و 4K") == set()


def test_urls_handles_hashtags_ignored():
    assert vals("https://x.com/a?b=12 @waltest0001 #tag12") == set()


def test_dates_are_separate_kind():
    figs = extract_figures("تاریخ ۱۴۰۵/۰۷/۲۰ و 12 October 2026")
    kinds = {f.kind for f in figs}
    assert kinds == {"date"}
    assert {f.date for f in figs} == {(1405, 7, 20), (2026, 10, 12)}


def test_word_numbers_with_units_only():
    assert Decimal(2) in vals("دو برابر شد")
    assert vals("یک محصول جدید") == set()
    assert vals("صد هزار تومان") == {Decimal(100000)}


def test_currency_detected():
    figs = extract_figures("قیمت $100 و 5 میلیون تومان")
    assert [f.kind for f in figs] == ["currency", "currency"]
