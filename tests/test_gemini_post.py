"""Text for /new: prompt, strict parsing, gate and daily text budget."""
import pytest

from app import gemini, gemini_post
from app.gemini import Selectors
from app.gemini_post import PostError

SAMPLE = """TITLE: مزایای انرژی
BODY: خانه‌های کوچک هم می‌توانند از انرژی خورشیدی استفاده کنند.
HASHTAGS: #انرژی تست, پایدار
CTA: بیشتر بخوانید
KEYWORDS: home solar energy, rooftop"""


def test_prompt_contains_brief_and_all_labels():
    p = gemini_post.build_prompt("انرژی خورشیدی")
    assert "انرژی خورشیدی" in p
    for label in ("TITLE:", "BODY:", "HASHTAGS:", "CTA:", "KEYWORDS:"):
        assert label in p


def test_parse_valid_answer():
    f = gemini_post.parse_post(SAMPLE)
    assert f["TITLE"] == "مزایای انرژی"
    assert f["HASHTAGS"] == "#انرژی #تست #پایدار"
    assert f["KEYWORDS"] == "home solar energy rooftop"
    assert f["CTA"] == "بیشتر بخوانید"


def test_missing_hashtags_get_default():
    f = gemini_post.parse_post("TITLE: t\nBODY: b\n")
    assert f["HASHTAGS"] == "#وال"


@pytest.mark.parametrize("garbage", ["", "TITLE: فقط عنوان"])
def test_unusable_answer_is_refused(garbage):
    with pytest.raises(PostError):
        gemini_post.parse_post(garbage)


def test_gate_needs_response_selector(monkeypatch):
    sel = Selectors("verified", {"prompt_box": "a", "send_button": "b", "image_result": "c"})
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: sel)
    with pytest.raises(PostError, match="سلکتور پاسخ"):
        gemini_post.check_ready(None)


def test_gate_open_when_all_selectors_present(monkeypatch):
    sel = Selectors("verified", {"prompt_box": "a", "send_button": "b", "image_result": "c",
                                 "response_text": "d"})
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: sel)
    gemini_post.check_ready(None)  # does not raise


def test_daily_text_budget(ctx):
    ctx.cfg.gemini.max_text_per_day = 2
    gemini_post.spend_text_budget(ctx)
    gemini_post.spend_text_budget(ctx)
    with pytest.raises(PostError, match="سقف روزانه"):
        gemini_post.spend_text_budget(ctx)


def test_markdown_bold_labels_are_accepted():
    text = "**TITLE:** مزایای انرژی\n**BODY:** خانه‌های کوچک هم می‌توانند از انرژی خورشیدی استفاده کنند.\n**HASHTAGS:** #انرژی #پایدار"
    f = gemini_post.parse_post(text)
    assert f["TITLE"] == "مزایای انرژی"
    assert f["HASHTAGS"] == "#انرژی #پایدار"


def test_persian_labels_are_accepted():
    text = "عنوان: مزایای انرژی\nمتن: خانه‌های کوچک هم می‌توانند از انرژی خورشیدی استفاده کنند.\nهشتگ‌ها: انرژی پایدار"
    f = gemini_post.parse_post(text)
    assert f["TITLE"] == "مزایای انرژی"
    assert f["BODY"].startswith("خانه‌های کوچک")
    assert f["HASHTAGS"] == "#انرژی #پایدار"


def test_multi_line_body_is_joined():
    text = "TITLE: عنوان\nBODY: خط اول بدنه\nخط دوم بدنه\nHASHTAGS: #تست"
    f = gemini_post.parse_post(text)
    assert f["BODY"] == "خط اول بدنه خط دوم بدنه"


def test_refusal_error_shows_start_of_answer():
    with pytest.raises(PostError, match="ابتدای پاسخ"):
        gemini_post.parse_post("متأسفم، نمی‌توانم این کار را انجام دهم.")


def test_free_text_answer_becomes_the_post():
    f = gemini_post.parse_post("مزایای ذخیره‌ی انرژی در خانه‌های کوچک را همین امروز تجربه کنید.")
    assert f["BODY"].startswith("مزایای ذخیره‌ی انرژی")
    assert f["TITLE"]


def test_persian_refusal_is_rejected_with_the_answer_start():
    with pytest.raises(PostError, match="رد کرد"):
        gemini_post.parse_post("نمی‌توانم کمکی کنم زیرا من فقط مدل زبان هستم و اطلاعات لازم را ندارم.")


def test_free_text_hashtags_are_pulled_out_of_the_body():
    f = gemini_post.parse_post("ذخیره‌ی انرژی در خانه مفید است. #انرژی #خانه")
    assert "#انرژی" in f["HASHTAGS"] and "#خانه" in f["HASHTAGS"]
    assert "#" not in f["BODY"]


def test_plain_prompt_variant_is_used_when_asked():
    assert gemini_post.build_prompt("انرژی", "plain").count("انرژی") == 1
    assert "TITLE" in gemini_post.build_prompt("انرژی")
    assert "TITLE" not in gemini_post.build_prompt("انرژی", "plain")
