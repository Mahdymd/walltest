"""ImageMode=gemini rows: generation is faked here; the gate, budget, pause and restart rules are real."""
import asyncio
import random

import pytest
from PIL import Image

from app import geministate, gemini, imagegen, pipeline
from app.gemini import Selectors
from app.policy import SourceResult
from app.sheets import COLUMNS
from tests.conftest import make_row

VERIFIED = Selectors(status="verified", elements={
    "prompt_box": "div.prompt", "send_button": "button.send",
    "image_result": "img.result", "busy_indicator": "div.busy",
})
CLOSED = Selectors(status="needs_verification", elements=dict(VERIFIED.elements))


def run(coro):
    return asyncio.run(coro)


def textured_png(path):
    rnd = random.Random(7)
    img = Image.new("RGB", (1024, 1024))
    px = img.load()
    for x in range(0, 1024, 8):
        for y in range(0, 1024, 8):
            c = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
            for dx in range(8):
                for dy in range(8):
                    px[x + dx, y + dy] = c
    img.save(path, "PNG")
    return path


@pytest.fixture
def no_network(monkeypatch):
    def fake_fetch(url, allowed, timeout_s, min_chars, proxy=""):
        return SourceResult(url=url, domain="en.wikipedia.org", ok=True, text="x")
    monkeypatch.setattr(pipeline, "fetch_source", fake_fetch)


@pytest.fixture
def images_dir(tmp_path, monkeypatch):
    d = tmp_path / "images"
    monkeypatch.setattr(pipeline, "IMAGES_DIR", d)
    return d


@pytest.fixture
def gate_open(monkeypatch):
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)


@pytest.fixture
def fake_gen(monkeypatch, tmp_path):
    calls = []

    def generate(prompt, out_dir, timeout_s, new_chat):  # noqa: ANN001
        calls.append({"prompt": prompt, "new_chat": new_chat})
        out_dir.mkdir(parents=True, exist_ok=True)
        return textured_png(out_dir / f"gemini_{len(calls)}.png")

    monkeypatch.setattr(imagegen, "generate_blocking", generate)
    return calls


def gemini_row(pid, **extra):
    base = dict(ID=pid, Status="Pending", Title="بازار ابزار", Body="متن ساده", Hashtags="#تست",
                ImageMode="gemini")
    base.update(extra)
    return make_row(**base)


def test_generated_image_is_processed_and_row_is_drafted(ctx, rec, backend, store, no_network,
                                                         gate_open, fake_gen, images_dir):
    backend.tabs["Content"].append(gemini_row("W-0200"))
    run(pipeline.sync_once(ctx, rec))
    post = store.get_post("W-0200")
    assert post["status"] == "Drafted"
    assert post["image_path"].endswith(".jpg") and "gemini" in post["image_path"]
    assert len(fake_gen) == 1 and fake_gen[0]["new_chat"] is False
    assert "بازار ابزار" in fake_gen[0]["prompt"]


def test_closed_gate_leaves_row_waiting_with_reason(ctx, rec, backend, store, no_network, monkeypatch,
                                                    fake_gen, images_dir):
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: CLOSED)
    backend.tabs["Content"].append(gemini_row("W-0201"))
    run(pipeline.sync_once(ctx, rec))
    post = store.get_post("W-0201")
    assert post["status"] == "Waiting"
    assert "تأیید نشده" in (post["last_error"] or "")
    assert fake_gen == []  # generation never started
    assert "alert" in rec.kinds()


def test_paused_rows_stay_pending_and_nothing_runs(ctx, rec, backend, store, no_network, gate_open,
                                                   fake_gen, images_dir):
    geministate.set_paused(store, True)
    backend.tabs["Content"].append(gemini_row("W-0202"))
    stats = run(pipeline.sync_once(ctx, rec))
    assert stats["paused"] == 1
    assert store.get_post("W-0202") is None
    assert backend.tabs["Content"][1][COLUMNS.index("Status")] == "Pending"
    assert fake_gen == []


def test_daily_limit_leaves_row_waiting(ctx, rec, backend, store, no_network, gate_open, fake_gen, images_dir):
    store.kv_set("gemini_day", geministate.today_key())
    store.kv_set("gemini_day_used", str(ctx.cfg.gemini.max_images_per_day))
    backend.tabs["Content"].append(gemini_row("W-0203"))
    run(pipeline.sync_once(ctx, rec))
    post = store.get_post("W-0203")
    assert post["status"] == "Waiting"
    assert "سقف روزانه" in (post["last_error"] or "")
    assert fake_gen == []


def test_restart_rule_passes_new_chat_flag(ctx, rec, backend, store, no_network, gate_open, fake_gen, images_dir):
    store.kv_set("gemini_since_restart", str(ctx.cfg.gemini.restart_every_images))
    backend.tabs["Content"].append(gemini_row("W-0204"))
    run(pipeline.sync_once(ctx, rec))
    assert fake_gen[0]["new_chat"] is True


def test_generation_timeout_is_waiting_not_failed(ctx, rec, backend, store, no_network, gate_open,
                                                  monkeypatch, images_dir):
    def timeout(prompt, out_dir, timeout_s, new_chat):  # noqa: ANN001
        raise imagegen.ImageGenError("Waiting", "Gemini در مهلت زمانی تصویری نساخت.")
    monkeypatch.setattr(imagegen, "generate_blocking", timeout)
    backend.tabs["Content"].append(gemini_row("W-0205"))
    run(pipeline.sync_once(ctx, rec))
    assert store.get_post("W-0205")["status"] == "Waiting"


def test_blank_generated_image_fails_with_persian_reason(ctx, rec, backend, store, no_network, gate_open,
                                                         monkeypatch, images_dir, tmp_path):
    def blank(prompt, out_dir, timeout_s, new_chat):  # noqa: ANN001
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "blank.png"
        Image.new("RGB", (1024, 1024), (128, 128, 128)).save(p)
        return p
    monkeypatch.setattr(imagegen, "generate_blocking", blank)
    backend.tabs["Content"].append(gemini_row("W-0206"))
    run(pipeline.sync_once(ctx, rec))
    post = store.get_post("W-0206")
    assert post["status"] == "Failed"
    assert "قابل استفاده نیست" in (post["last_error"] or "")


def test_prompt_override_and_template(ctx):
    values = {"Title": "تلگرام", "ImageStyle": "3d", "ImagePrompt": ""}
    p = imagegen.build_prompt(ctx.cfg, values)
    assert "تلگرام" in p and "clean 3D render" in p and "no text" in p.lower()
    values["ImagePrompt"] = "a red bicycle"
    assert imagegen.build_prompt(ctx.cfg, values) == "a red bicycle"
