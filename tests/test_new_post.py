"""Telegram /new end to end with fakes: text, licensed photo or generated photo, sheet row, preview."""
import asyncio
from types import SimpleNamespace

import pytest

from app import commons, gemini, gemini_post, imagegen, pipeline, telegram_bot
from app.commons import Hit
from app.gemini import Selectors
from app.sheets import COLUMNS
from tests.test_pipeline_gemini import textured_png

SAMPLE = """TITLE: مزایای انرژی
BODY: خانه‌های کوچک هم می‌توانند از انرژی خورشیدی استفاده کنند.
HASHTAGS: #انرژی #پایدار
CTA: بیشتر بخوانید
KEYWORDS: home solar energy"""

OPEN = Selectors("verified", {"prompt_box": "a", "send_button": "b", "image_result": "c", "response_text": "d"})
CLOSED = Selectors("needs_verification", {"prompt_box": "a", "send_button": "b", "image_result": "c",
                                          "response_text": "d"})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def text_ready(monkeypatch):
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: OPEN)


@pytest.fixture
def writer(monkeypatch):
    calls = []

    def write(brief, timeout_s, variant="structured"):  # noqa: ANN001
        calls.append(brief)
        return SAMPLE
    monkeypatch.setattr(gemini_post, "write_post_blocking", write)
    return calls


@pytest.fixture
def images(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "IMAGES_DIR", tmp_path / "images")
    return tmp_path


@pytest.fixture
def sources_ok(backend):
    backend.tabs["Sources"].append(["commons.wikimedia.org"])


def test_licensed_photo_is_used_and_credited(ctx, rec, backend, store, text_ready, writer, images,
                                             sources_ok, monkeypatch):
    raw = textured_png(images / "src.png")
    hit = Hit(page_url="https://commons.wikimedia.org/wiki/File:Solar.jpg",
              image_url="https://upload.wikimedia.org/solar.jpg", width=1200, height=800,
              license="CC BY-SA 4.0", author="Sara")
    seen = {}

    def find(terms, dest_dir, **kw):  # noqa: ANN001, ANN003
        seen["terms"] = terms
        return hit, raw
    monkeypatch.setattr(commons, "find_photo", find)

    out = run(pipeline.new_post_from_brief(ctx, rec, "مزایای انرژی خورشیدی"))
    assert out.status == "Drafted", out.message
    assert out.post_id == "W-0001"
    assert writer == ["مزایای انرژی خورشیدی"]
    assert seen["terms"] == "home solar energy"

    post = store.get_post("W-0001")
    assert post["status"] == "Drafted"
    assert "commons" in post["image_path"]
    assert post["caption_html"] and "commons.wikimedia.org" in post["caption_html"]

    run(pipeline.flush_now(ctx))
    row = backend.tabs["Content"][1]
    assert row[COLUMNS.index("ID")] == "W-0001"
    assert row[COLUMNS.index("Status")] == "Drafted"
    assert row[COLUMNS.index("SourceURL")] == hit.page_url
    assert row[COLUMNS.index("ImageMode")] == "auto"


def test_generated_image_when_no_licensed_photo(ctx, rec, backend, store, text_ready, writer, images,
                                                monkeypatch):
    monkeypatch.setattr(commons, "find_photo", lambda *a, **k: None)
    calls = []

    def generate(prompt, out_dir, timeout_s, new_chat):  # noqa: ANN001
        calls.append(new_chat)
        out_dir.mkdir(parents=True, exist_ok=True)
        return textured_png(out_dir / "gen.png")
    monkeypatch.setattr(imagegen, "generate_blocking", generate)
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: OPEN)

    out = run(pipeline.new_post_from_brief(ctx, rec, "مزایای انرژی خورشیدی"))
    assert out.status == "Drafted", out.message
    assert len(calls) == 1
    assert "gemini" in store.get_post("W-0001")["image_path"]


def test_closed_gate_refuses_before_writing_anything(ctx, rec, backend, store, writer, images, monkeypatch):
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: CLOSED)
    out = run(pipeline.new_post_from_brief(ctx, rec, "موضوع آزمایشی"))
    assert out.status == "Failed" and "تأیید نشده" in out.message
    assert writer == []
    assert len(backend.tabs["Content"]) == 1  # header only, no row appended


def test_unusable_gemini_answer_creates_no_row(ctx, rec, backend, store, text_ready, images, monkeypatch):
    monkeypatch.setattr(gemini_post, "write_post_blocking", lambda brief, timeout_s, variant="structured": "نمی‌توانم کمکی کنم، من فقط مدل زبان هستم.")
    out = run(pipeline.new_post_from_brief(ctx, rec, "موضوع آزمایشی"))
    assert out.status == "Failed"
    assert len(backend.tabs["Content"]) == 1


def test_new_command_refuses_short_brief(ctx, store):
    msg = SimpleNamespace(texts=[])

    async def reply(text, **kw):  # noqa: ANN003
        msg.texts.append(text)
    msg.reply_text = reply
    update = SimpleNamespace(effective_user=SimpleNamespace(id=111), effective_message=msg, callback_query=None)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": ctx}), args=["hi"])
    run(telegram_bot.cmd_new(update, context))
    assert "مثال" in msg.texts[-1]


def test_pause_does_not_block_a_licensed_photo(ctx, rec, backend, store, text_ready, writer, images,
                                               sources_ok, monkeypatch):
    from app import geministate
    geministate.set_paused(store, True)
    raw = textured_png(images / "src.png")
    hit = Hit(page_url="https://commons.wikimedia.org/wiki/File:S.jpg",
              image_url="https://upload.wikimedia.org/s.jpg", width=1200, height=800,
              license="CC0", author="Ana")
    monkeypatch.setattr(commons, "find_photo", lambda *a, **k: (hit, raw))
    out = run(pipeline.new_post_from_brief(ctx, rec, "مزایای انرژی خورشیدی"))
    assert out.status == "Drafted", out.message


def test_pause_stops_generation_when_no_photo(ctx, rec, backend, store, text_ready, writer, images,
                                              monkeypatch):
    from app import geministate
    geministate.set_paused(store, True)
    monkeypatch.setattr(commons, "find_photo", lambda *a, **k: None)

    def never(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("generation must not run while paused")
    monkeypatch.setattr(imagegen, "generate_blocking", never)
    out = run(pipeline.new_post_from_brief(ctx, rec, "مزایای انرژی خورشیدی"))
    assert out.status == "Waiting" and "متوقف" in out.message
