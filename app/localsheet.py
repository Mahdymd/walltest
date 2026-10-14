"""Local queue that replaces Google Sheets (sheet.backend = "local").

Each tab is a CSV file in data/sheet/ (UTF-8 with BOM, so Excel opens it correctly).
It implements the same read_tab / write_cells interface as the Google backend, so the
pipeline, outbox and status machine work unchanged.
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
import threading
from pathlib import Path


class LocalBackend:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, tab: str) -> Path:
        return self.folder / f"{tab}.csv"

    def read_tab(self, tab: str) -> list[list[str]] | None:
        p = self.path(tab)
        if not p.exists():
            return None
        with self._lock:
            text = p.read_text(encoding="utf-8-sig")
        return [list(r) for r in csv.reader(io.StringIO(text))]

    def write_cells(self, tab: str, updates: list[tuple[int, int, str]]) -> None:
        if not updates:
            return
        p = self.path(tab)
        with self._lock:
            grid = [list(r) for r in csv.reader(io.StringIO(p.read_text(encoding="utf-8-sig")))] if p.exists() else []
            for row, col, value in updates:
                while len(grid) < row:
                    grid.append([])
                r = grid[row - 1]
                while len(r) < col:
                    r.append("")
                r[col - 1] = value
            self._write_atomic(p, grid)

    def ensure_tab(self, tab: str, headers: list[str]) -> bool:
        """Create the tab with a header row if it does not exist. Returns True when created."""
        p = self.path(tab)
        if p.exists():
            return False
        with self._lock:
            self._write_atomic(p, [list(headers)])
        return True

    @staticmethod
    def _write_atomic(p: Path, grid: list[list[str]]) -> None:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(grid)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(buf.getvalue())
            try:
                os.replace(tmp, p)
            except PermissionError as exc:
                # Excel keeps the file locked while it is open on Windows
                raise RuntimeError(f"فایل «{p.name}» باز است (احتمالاً در Excel). آن را ببندید؛ ربات دوباره تلاش می‌کند.") from exc
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
