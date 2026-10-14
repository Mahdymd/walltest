"""Google Sheets adapter: header-mapped reads, outbox-based batch writes, conflict hashing.

Blocking I/O lives here; callers run it with asyncio.to_thread.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .store import Store

log = logging.getLogger(__name__)

COLUMNS = [
    "ID", "Status", "Title", "Body", "FullText", "SourceURL", "Hashtags", "CTA",
    "ImageMode", "ImageStyle", "ImagePrompt", "AdminNote", "RejectReason",
    "PolicyReport", "Error", "PostURL", "UpdatedAt", "PublishedAt",
]
HUMAN_COLUMNS = ["Title", "Body", "FullText", "SourceURL", "Hashtags", "CTA",
                 "ImageMode", "ImageStyle", "ImagePrompt"]
BOT_COLUMNS = [c for c in COLUMNS if c not in HUMAN_COLUMNS and c != "ID"]
STATUS_VALUES = ["Pending", "Processing", "Drafted", "Publishing", "Published", "NeedsFix",
                 "Rejected", "Deleted", "Waiting", "Failed"]
IMAGE_MODES = ["gemini", "none", "auto"]  # auto = search licensed photo, then Gemini
IMAGE_STYLES = ["photo", "3d", "illustration", "minimal", "infographic-bg"]
BANNED_HEADERS = ["Word", "Level", "Note"]
SOURCES_HEADERS = ["Domain"]
ID_RE = re.compile(r"^W-\d{4,}$")


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def clean(v: object) -> str:
    return str(v if v is not None else "").replace("\r\n", "\n").strip()


def human_hash(values: dict[str, str]) -> str:
    payload = [clean(values.get(c, "")) for c in HUMAN_COLUMNS]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def fulltext_hash(values: dict[str, str]) -> str:
    return hashlib.sha256(clean(values.get("FullText", "")).encode("utf-8")).hexdigest()[:16]


def missing_headers(headers: list[str], required: list[str] = COLUMNS) -> list[str]:
    have = {h.strip() for h in headers}
    return [c for c in required if c not in have]


@dataclass
class Row:
    sheet_row: int  # 1-based row number in the sheet
    values: dict[str, str]

    @property
    def id(self) -> str:
        return clean(self.values.get("ID", "")).upper()

    @property
    def status(self) -> str:
        return clean(self.values.get("Status", ""))


@dataclass
class Snapshot:
    headers: list[str]
    rows: list[Row]
    col_index: dict[str, int] = field(default_factory=dict)  # header -> 1-based column
    by_id: dict[str, Row] = field(default_factory=dict)

    def find(self, post_id: str) -> Row | None:
        return self.by_id.get(post_id)


def build_snapshot(table: list[list[str]]) -> Snapshot:
    if not table:
        return Snapshot(headers=[], rows=[])
    headers = [h.strip() for h in table[0]]
    col_index: dict[str, int] = {}
    for i, h in enumerate(headers, start=1):
        if h and h not in col_index:
            col_index[h] = i
    rows: list[Row] = []
    by_id: dict[str, Row] = {}
    for n, raw in enumerate(table[1:], start=2):
        values = {h: clean(raw[col_index[h] - 1]) if col_index[h] - 1 < len(raw) else "" for h in col_index}
        if not any(values.values()):
            continue
        row = Row(sheet_row=n, values=values)
        rows.append(row)
        if row.id:
            by_id.setdefault(row.id, row)
    return Snapshot(headers=headers, rows=rows, col_index=col_index, by_id=by_id)


class Backend(Protocol):
    def read_tab(self, tab: str) -> list[list[str]] | None: ...
    def write_cells(self, tab: str, updates: list[tuple[int, int, str]]) -> None: ...


class MemoryBackend:
    """In-memory sheet for tests and --dry-run experiments."""

    def __init__(self, tabs: dict[str, list[list[str]]] | None = None) -> None:
        self.tabs: dict[str, list[list[str]]] = tabs or {}
        self.writes = 0
        self.fail_next = False

    def read_tab(self, tab: str) -> list[list[str]] | None:
        if tab not in self.tabs:
            return None
        return [list(r) for r in self.tabs[tab]]

    def write_cells(self, tab: str, updates: list[tuple[int, int, str]]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("429 Too Many Requests (fake)")
        grid = self.tabs[tab]
        for row, col, value in updates:
            while len(grid) < row:
                grid.append([])
            r = grid[row - 1]
            while len(r) < col:
                r.append("")
            r[col - 1] = value
            self.writes += 1


class GSpreadBackend:
    def __init__(self, service_account_file: Path, sheet_id: str) -> None:
        import gspread  # local import keeps tests light

        self._gspread = gspread
        gc = gspread.service_account(filename=str(service_account_file))
        self.sheet = gc.open_by_key(sheet_id)

    def worksheet(self, tab: str):  # noqa: ANN201
        try:
            return self.sheet.worksheet(tab)
        except self._gspread.exceptions.WorksheetNotFound:
            return None

    def read_tab(self, tab: str) -> list[list[str]] | None:
        ws = self.worksheet(tab)
        if ws is None:
            return None
        return ws.get_all_values()

    def write_cells(self, tab: str, updates: list[tuple[int, int, str]]) -> None:
        ws = self.worksheet(tab)
        if ws is None:
            raise RuntimeError(f"تب «{tab}» در شیت پیدا نشد")
        data = [{"range": f"{col_letter(c)}{r}", "values": [[v]]} for r, c, v in updates]
        ws.batch_update(data, value_input_option="RAW")


class SheetClient:
    """Reads and writes the Content tab (and optional BannedWords/Sources tabs)."""

    def __init__(self, backend: Backend, tab_content: str = "Content",
                 tab_banned: str = "BannedWords", tab_sources: str = "Sources") -> None:
        self.backend = backend
        self.tab_content = tab_content
        self.tab_banned = tab_banned
        self.tab_sources = tab_sources
        self.last: Snapshot | None = None

    def read_content(self) -> Snapshot:
        table = self.backend.read_tab(self.tab_content)
        if table is None:
            raise RuntimeError(f"تب «{self.tab_content}» در شیت پیدا نشد")
        snap = build_snapshot(table)
        self.last = snap
        return snap

    def read_banned(self) -> list[tuple[str, str]] | None:
        table = self.backend.read_tab(self.tab_banned)
        if table is None:
            return None
        out: list[tuple[str, str]] = []
        headers = [h.strip() for h in table[0]] if table else []
        if "Word" not in headers or "Level" not in headers:
            return []
        wi, li = headers.index("Word"), headers.index("Level")
        for r in table[1:]:
            if len(r) > max(wi, li) and clean(r[wi]):
                out.append((clean(r[wi]), clean(r[li]).lower()))
        return out

    def append_row(self, values: dict[str, str]) -> int:
        """Write one new row at the bottom of the Content tab. Returns the sheet row number."""
        table = self.backend.read_tab(self.tab_content)
        if table is None:
            raise RuntimeError(f"تب «{self.tab_content}» در شیت پیدا نشد")
        snap = build_snapshot(table)
        row_no = len(table) + 1
        updates: list[tuple[int, int, str]] = []
        for name, value in values.items():
            col = snap.col_index.get(name)
            if col is None:
                raise RuntimeError(f"ستون «{name}» در شیت پیدا نشد")
            if value != "":
                updates.append((row_no, col, value))
        self.backend.write_cells(self.tab_content, updates)
        self.last = None
        return row_no

    def read_sources(self) -> list[str] | None:
        table = self.backend.read_tab(self.tab_sources)
        if table is None:
            return None
        headers = [h.strip() for h in table[0]] if table else []
        if "Domain" not in headers:
            return []
        di = headers.index("Domain")
        return [clean(r[di]).lower() for r in table[1:] if len(r) > di and clean(r[di])]


def flush_outbox(store: Store, client: SheetClient, now_ts: float, batch: int = 200) -> tuple[int, int]:
    """Write queued cell updates. Returns (written, failed). Runs blocking I/O."""
    due = store.outbox_due(batch)
    if not due:
        return (0, 0)
    snap = client.last
    if snap is None:
        snap = client.read_content()
    # rows whose ID is still being written in this same batch (new rows)
    hinted: dict[str, int] = {}
    for entry in due:
        if entry["row_hint"] and entry["column_name"] == "ID":
            hinted[entry["post_id"]] = entry["row_hint"]

    def resolve(entry: dict) -> Row | None:
        pid = entry["post_id"]
        row = snap.find(pid) if pid else None
        if row is None and pid in hinted:
            row = Row(sheet_row=hinted[pid], values={})
        return row

    resolved: list[tuple[dict, Row | None]] = [(e, resolve(e)) for e in due]
    if any(row is None for _, row in resolved):
        snap = client.read_content()
        resolved = [(e, resolve(e)) for e, _ in resolved]
    updates: list[tuple[int, int, str]] = []
    ok_ids: list[int] = []
    failed: list[dict] = []
    for entry, row in resolved:
        col = snap.col_index.get(entry["column_name"])
        if row is None or col is None:
            failed.append(entry)
            continue
        updates.append((row.sheet_row, col, entry["value"]))
        ok_ids.append(entry["id"])
    for entry in failed:
        store.outbox_fail([entry["id"]], "سطر یا ستون در شیت پیدا نشد", delay=300)
    if not updates:
        return (0, len(failed))
    try:
        client.backend.write_cells(client.tab_content, updates)
    except Exception as exc:  # noqa: BLE001 - classify below
        msg = str(exc)
        delay = 60.0 if "429" in msg or "quota" in msg.lower() else 30.0
        store.outbox_fail(ok_ids, f"نوشتن در شیت ناموفق: {msg[:200]}", delay=delay)
        log.warning("sheet write failed: %s", msg[:200])
        return (0, len(ok_ids) + len(failed))
    store.outbox_done(ok_ids)
    # the snapshot is stale now; the next read will refresh it
    client.last = None
    return (len(ok_ids), len(failed))
