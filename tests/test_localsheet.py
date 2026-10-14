"""Local CSV queue (sheet.backend = "local") replaces Google Sheets."""
import dataclasses

import pytest

from app import context, health
from app.localsheet import LocalBackend
from app.sheets import COLUMNS, SheetClient


def test_roundtrip_keeps_persian_commas_and_newlines(tmp_path):
    b = LocalBackend(tmp_path)
    b.ensure_tab("Content", COLUMNS)
    b.write_cells("Content", [(2, 1, "W-0001"), (2, 2, "عنوان، با ویرگول"), (2, 3, "خط اول\nخط دوم")])
    again = LocalBackend(tmp_path)  # a fresh instance reads the same file
    rows = again.read_tab("Content")
    assert rows[0] == COLUMNS
    assert rows[1][:3] == ["W-0001", "عنوان، با ویرگول", "خط اول\nخط دوم"]


def test_file_is_utf8_with_bom_so_excel_reads_persian(tmp_path):
    b = LocalBackend(tmp_path)
    b.ensure_tab("Content", COLUMNS)
    assert (tmp_path / "Content.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_missing_tab_returns_none_and_ensure_is_idempotent(tmp_path):
    b = LocalBackend(tmp_path)
    assert b.read_tab("Sources") is None
    assert b.ensure_tab("Content", COLUMNS) is True
    assert b.ensure_tab("Content", COLUMNS) is False


def test_sheet_client_works_on_local_backend(tmp_path):
    b = LocalBackend(tmp_path)
    b.ensure_tab("Content", COLUMNS)
    client = SheetClient(b)
    row = client.append_row({"ID": "W-0001", "Status": "Pending", "Title": "تست"})
    assert row == 2
    snap = client.read_content()
    assert snap.find("W-0001").status == "Pending"


def test_build_context_uses_local_csv_by_default(cfg, store, env, tmp_path, monkeypatch):
    monkeypatch.setattr(context, "SHEET_DIR", tmp_path / "sheet")
    assert cfg.sheet.backend == "local"
    ctx = context.build_context(cfg, env, store)
    assert isinstance(ctx.sheet.backend, LocalBackend)
    assert (tmp_path / "sheet" / "Content.csv").exists()


def test_google_backend_is_not_touched_in_local_mode(cfg, store, env, tmp_path, monkeypatch):
    monkeypatch.setattr(context, "SHEET_DIR", tmp_path / "sheet")

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("Google backend must not be created in local mode")
    monkeypatch.setattr(context, "GSpreadBackend", boom)
    context.build_context(cfg, env, store)


def test_google_mode_still_needs_sheet_id(cfg, store, env):
    cfg.sheet.backend = "google"
    env = dataclasses.replace(env, sheet_id="")
    with pytest.raises(RuntimeError, match="GOOGLE_SHEET_ID"):
        context.make_backend(cfg, env)


def test_unknown_backend_is_refused(cfg, env):
    cfg.sheet.backend = "excel-online"
    with pytest.raises(RuntimeError, match="sheet.backend"):
        context.make_backend(cfg, env)


def test_health_skips_google_settings_in_local_mode(cfg, env):
    env = dataclasses.replace(env, sheet_id="")
    names = [c.name for c in health.check_env(env, cfg)]
    assert "GOOGLE_SHEET_ID" not in names
    cfg.sheet.backend = "google"
    assert "GOOGLE_SHEET_ID" in [c.name for c in health.check_env(env, cfg)]


def test_health_check_for_local_queue_is_ok(cfg, env, tmp_path, monkeypatch):
    monkeypatch.setattr(context, "SHEET_DIR", tmp_path / "sheet")
    checks = health.check_sheet(env, cfg)
    assert checks[0].status == "ok"
    assert all(c.status != "fail" for c in checks)
