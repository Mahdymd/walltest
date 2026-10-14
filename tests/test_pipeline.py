import asyncio

import pytest

from app import pipeline
from app.sheets import COLUMNS
from app.policy import SourceResult
from tests.conftest import make_row


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def no_network(monkeypatch):
    def fake_fetch(url, allowed, timeout_s, min_chars, proxy=""):
        return SourceResult(url=url, domain="en.wikipedia.org", ok=True,
                            text="Telegram was launched in August 2013 by Pavel Durov.")
    monkeypatch.setattr(pipeline, "fetch_source", fake_fetch)


def test_pending_row_becomes_draft_and_sheet_gets_status(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0001", Status="Pending", Title="سلام",
                                            Body="متن ساده", Hashtags="#تست", ImageMode="none"))
    stats = run(pipeline.sync_once(ctx, rec))
    assert stats["processed"] == 1
    post = store.get_post("W-0001")
    assert post["status"] == "Drafted" and post["version"] == 1
    assert ("draft_ready", "W-0001") in rec.events
    assert backend.tabs["Content"][1][COLUMNS.index("Status")] == "Drafted"


def test_number_without_source_is_needs_fix_no_preview(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0002", Status="Pending", Title="ادعا",
                                            Body="رشد ۴۰٪ی بازار", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    assert store.get_post("W-0002")["status"] == "NeedsFix"
    assert "blocked" in rec.kinds() and "draft_ready" not in rec.kinds()


def test_number_with_source_found_passes(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(
        ID="W-0003", Status="Pending", Title="تلگرام", Body="تلگرام در سال ۲۰۱۳ راه‌اندازی شد.",
        SourceURL="https://en.wikipedia.org/wiki/Telegram_(software)", Hashtags="#تلگرام", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    post = store.get_post("W-0003")
    assert post["status"] == "Drafted"
    assert post["report_json"] and '"verdict"' not in post["report_json"]


def test_ids_assigned_to_rows_without_id(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(Status="", Title="اول", Body="متن", ImageMode="none"))
    backend.tabs["Content"].append(make_row(Status="", Title="دوم", Body="متن", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    ids = [r[0] for r in backend.tabs["Content"][1:]]
    assert ids == ["W-0001", "W-0002"]


def test_only_pending_rows_are_processed_top_down(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0010", Status="Published", Title="x", Body="y", ImageMode="none"))
    backend.tabs["Content"].append(make_row(ID="W-0011", Status="Pending", Title="a", Body="b", ImageMode="none"))
    backend.tabs["Content"].append(make_row(ID="W-0012", Status="Pending", Title="c", Body="d", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    drafted = [e[1] for e in rec.events if e[0] == "draft_ready"]
    assert drafted == ["W-0011", "W-0012"]
    assert store.get_post("W-0010") is None


def test_max_pending_drafts_leaves_rest_pending(ctx, rec, backend, store, no_network):
    ctx.cfg.sheet.max_pending_drafts = 1
    backend.tabs["Content"].append(make_row(ID="W-0021", Status="Pending", Title="a", Body="b", ImageMode="none"))
    backend.tabs["Content"].append(make_row(ID="W-0022", Status="Pending", Title="c", Body="d", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    assert store.get_post("W-0021")["status"] == "Drafted"
    assert store.get_post("W-0022") is None  # never touched, no Gemini quota spent later either


def test_gemini_mode_without_image_waits(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0030", Status="Pending", Title="a", Body="b", ImageMode=""))
    run(pipeline.sync_once(ctx, rec))
    assert store.get_post("W-0030")["status"] == "Waiting"
    assert "alert" in rec.kinds()


def test_empty_text_fails(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0031", Status="Pending", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    assert store.get_post("W-0031")["status"] == "Failed"


def test_human_change_marks_draft_stale(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0040", Status="Pending", Title="a", Body="b", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    run(pipeline.flush_now(ctx))
    # human edits the body in the sheet
    idx = COLUMNS.index("Body") 
    backend.tabs["Content"][1][idx] = "متن جدید"
    run(pipeline.sync_once(ctx, rec))
    assert store.get_post("W-0040")["stale"] == 1
    assert any(e[0] == "stale" for e in rec.events)


def test_redraft_increments_version_and_keeps_image(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0050", Status="Pending", Title="a", Body="b", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    run(pipeline.flush_now(ctx))
    out = run(pipeline.edit_text(ctx, rec, "W-0050", "متن <b>جدید</b>", "admin:111"))
    assert out.status == "Drafted" and out.version == 2
    assert store.get_post("W-0050")["version"] == 2
    assert "FullText" in COLUMNS and backend.tabs["Content"][1][COLUMNS.index("FullText")].startswith("متن")


def test_text_edit_conflict_detected(ctx, rec, backend, store, no_network):
    backend.tabs["Content"].append(make_row(ID="W-0060", Status="Pending", Title="a", Body="b", ImageMode="none"))
    run(pipeline.sync_once(ctx, rec))
    run(pipeline.flush_now(ctx))
    backend.tabs["Content"][1][COLUMNS.index("FullText")] = "شیت هم تغییر کرد"
    res = run(pipeline.edit_text(ctx, rec, "W-0060", "نسخه‌ی تلگرام", "admin:111"))
    assert res == "conflict"


def test_dry_run_writes_nothing(ctx, store, backend):
    row = make_row(ID="W-0070", Status="Pending", Title="a", Body="b", ImageMode="none")
    values = dict(zip(COLUMNS, row))
    ev = pipeline.dry_run(ctx, "W-0070", values, None)
    assert ev.status == "Drafted"
    assert store.get_post("W-0070") is None
