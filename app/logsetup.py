"""Logging with redaction of secrets (tokens, cookies, emails, profile paths)."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), "<TOKEN>"),
    (re.compile(r"(?i)(set-cookie|cookie)\s*[:=]\s*[^\s;]+"), r"\1=<COOKIE>"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "<EMAIL>"),
    (re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"), "<PRIVATE_KEY>"),
    (re.compile(r"(?i)[A-Z]:\\[^\s'\"]*chrome_profile[^\s'\"]*"), "<PROFILE>"),
    (re.compile(r"/[^\s'\"]*chrome_profile[^\s'\"]*"), "<PROFILE>"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = redact(msg)
        record.args = ()
        return True


def setup_logging(log_path: Path, max_mb: int = 5, backups: int = 5, console: bool = True) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=max_mb * 1024 * 1024, backupCount=backups, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(RedactFilter())
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(RedactFilter())
        root.addHandler(ch)
    # httpx logs full URLs including the bot token on every request; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
