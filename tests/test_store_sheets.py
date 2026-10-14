import pytest

from app.sheets import COLUMNS, MemoryBackend, SheetClient, flush_outbox, human_hash, missing_headers
from app.store import InvalidTransition
from tests.conftest import make_row


def test_valid_and_invalid_transitions(store):
    store.create_post("W-0001")
    assert store.transition("W-0001", "Pending", "Processing")
    with pytest.raises(InvalidTransition):
        store.transition("W-0001", "Published", "Pending")
    with pytest.raises(InvalidTransition):
        store.transition("W-0001", "Rejected", "Drafted")


def test_double_click_only_one_wins(store):
    store.create_post("W-0002")
    store.transition("W-0002", "Pending", "Processing")
    store.transition("W-0002", "Processing", "Drafted", version=None)
    store.set_fields("W-0002", version=1)
    first = store.transition("W-0002", "Drafted", "Publishing", version=1)
    second = store.transition("W-0002", "Drafted", "Publishing", version=1)
    assert first is True and second is False


def test_version_mismatch_blocks_transition(store):
    store.create_post("W-0003")
    store.set_fields("W-0003", version=2, status="Drafted")
    assert store.transition("W-0003", "Drafted", "Publishing", version=1) is False


def test_outbox_survives_and_flushes(store, backend):
    store.create_post("W-0004")
    store.outbox_add("W-0004", "Status", "Drafted")
    backend.tabs["Content"].append(make_row(ID="W-0004", Status="Pending"))
    client = SheetClient(backend)
    client.read_content()
    headers = client.last.headers
    assert headers[:2] == ["ID", "Status"]
    written, failed = flush_outbox(store, client, 0)
    assert written == 1 and failed == 0
    assert backend.tabs["Content"][1][COLUMNS.index("Status")] == "Drafted"
    assert store.outbox_count() == 0


def test_outbox_retries_after_backend_failure(store, backend):
    store.create_post("W-0005")
    store.outbox_add("W-0005", "Status", "Drafted")
    backend.tabs["Content"].append(make_row(ID="W-0005", Status="Pending"))
    backend.fail_next = True
    client = SheetClient(backend)
    client.read_content()
    flush_outbox(store, client, 0)
    assert store.outbox_count() == 1  # kept, not lost
    assert store.outbox_due() == []   # backoff in place
    assert store.outbox_for("W-0005") == 1


def test_headers_found_by_name_not_position(backend):
    # reversed column order + extra user column: must still work
    reversed_cols = list(reversed(COLUMNS)) + ["MyExtra"]
    grid = [reversed_cols]
    row = {c: "" for c in reversed_cols}
    row.update({"ID": "W-0010", "Status": "Pending", "Title": "تیتر", "Body": "متن"})
    grid.append([row[c] for c in reversed_cols])
    backend.tabs["Content"] = grid
    client = SheetClient(backend)
    snap = client.read_content()
    r = snap.find("W-0010")
    assert r is not None and r.values["Title"] == "تیتر"
    assert missing_headers(reversed_cols) == []


def test_missing_required_header_detected():
    assert "PolicyReport" in missing_headers([c for c in COLUMNS if c != "PolicyReport"])


def test_human_hash_changes_only_on_human_columns():
    base = {c: "" for c in COLUMNS}
    base.update({"Title": "a"})
    h1 = human_hash(base)
    changed_bot = dict(base, Status="Drafted", PolicyReport="⚠️1")
    assert human_hash(changed_bot) == h1
    changed_human = dict(base, Title="b")
    assert human_hash(changed_human) != h1


def test_banned_and_sources_tabs_parsed(backend):
    client = SheetClient(backend)
    assert ("کلاهبرداری", "hard") in client.read_banned()
    assert client.read_sources() == ["en.wikipedia.org"]


def test_missing_tabs_return_none():
    client = SheetClient(MemoryBackend({"Content": [list(COLUMNS)]}))
    assert client.read_banned() is None and client.read_sources() is None


def test_rows_without_content_are_ignored(backend):
    backend.tabs["Content"].append(make_row())
    backend.tabs["Content"].append(make_row(ID="W-0011", Status="Pending", Title="x"))
    snap = SheetClient(backend).read_content()
    assert [r.id for r in snap.rows] == ["W-0011"]
