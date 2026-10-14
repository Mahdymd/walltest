from app.sheet_init import dropdown_request, header_requests, missing_in, sample_row, status_color_requests, NOTES
from app.sheets import COLUMNS, STATUS_VALUES


def test_every_column_has_persian_note():
    for col in COLUMNS:
        assert NOTES.get(col), f"note missing for {col}"
        assert any("\u0600" <= ch <= "\u06FF" for ch in NOTES[col])


def test_header_requests_write_names_and_notes_at_offset():
    reqs = header_requests(7, 3, ["ID", "Status"])
    first = reqs[0]["updateCells"]
    assert first["start"] == {"sheetId": 7, "rowIndex": 0, "columnIndex": 2}
    assert first["rows"][0]["values"][0]["userEnteredValue"]["stringValue"] == "ID"
    assert first["rows"][0]["values"][0]["note"]


def test_missing_in_only_reports_absent_headers():
    assert missing_in(["ID", "Title"], ["ID", "Title", "Body"]) == ["Body"]


def test_dropdown_covers_all_statuses():
    req = dropdown_request(1, 1, STATUS_VALUES)
    values = [v["userEnteredValue"] for v in req["setDataValidation"]["rule"]["condition"]["values"]]
    assert set(values) == set(STATUS_VALUES)


def test_status_colors_one_rule_per_status():
    reqs = status_color_requests(1, 1)
    assert len(reqs) >= 8


def test_sample_row_follows_headers_order():
    headers = ["Body", "ID", "Title"]
    assert sample_row(headers, {"Title": "t", "Body": "b"}) == ["b", "", "t"]
