"""Configuration loading: .env (secrets) + config/config.json (behaviour)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "wal.db"
LOG_DIR = DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "wal.log"
IMAGES_DIR = DATA_DIR / "images"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
BACKUPS_DIR = DATA_DIR / "backups"
SHEET_DIR = DATA_DIR / "sheet"  # local queue (sheet.backend = "local")
INBOX_DIR = DATA_DIR / "inbox"  # topics.txt: one topic per line, processed automatically
CACHE_DIR = DATA_DIR / "cache"
LOCK_PATH = DATA_DIR / "wal.lock"
LOGO_PATH = ROOT / "assets" / "logo.png"
BANNED_TXT_PATH = ROOT / "config" / "banned_words.txt"
VERSION = "2.0.0-phase2"


class LogoCfg(BaseModel):
    width_ratio: float = 0.14
    opacity: float = 0.85
    margin: int = 28
    auto_contrast: bool = True
    contrast_min: int = 60


class ImagesCfg(BaseModel):
    width: int = 1280
    height: int = 720
    min_side: int = 512
    jpeg_quality: int = 90
    max_bytes: int = 1_000_000
    duplicate_hamming: int = 5
    recent_compare: int = 50
    logo: LogoCfg = LogoCfg()


class ChannelCfg(BaseModel):
    handle: str = "@waltest0001"
    display_name: str = "وال"


class SheetCfg(BaseModel):
    backend: str = "local"  # "local" = CSV files in data/sheet; "google" = Google Sheets API
    tab_content: str = "Content"
    tab_banned: str = "BannedWords"
    tab_sources: str = "Sources"
    poll_seconds: int = 90
    max_pending_drafts: int = 5


class TopicsCfg(BaseModel):
    enabled: bool = True
    interval_minutes: int = 60  # one topic is written per interval
    first_run_minutes: int = 2


class CaptionCfg(BaseModel):
    max_chars_image: int = 1000
    max_chars_text: int = 4096
    title_fallback_chars: int = 120
    title_format: str = "<b>{title}</b>"
    source_label_format: str = '🔗 منبع: <a href="{url}">{label}</a>'


class PolicyCfg(BaseModel):
    hashtags_min: int = 1
    hashtags_max: int = 6
    max_emojis: int = 8
    max_repeated_marks: int = 2
    max_line_chars: int = 400
    latin_only_review: bool = True
    flag_emojis_review: bool = False
    duplicate_threshold: float = 0.85
    duplicate_days: int = 30
    shorteners: list[str] = []


class SourcesCfg(BaseModel):
    timeout_seconds: int = 15
    cache_hours: int = 24
    min_text_chars: int = 500
    allowed_domains_fallback: list[str] = []


class ImagePromptCfg(BaseModel):
    template: str = ""
    style_fragments: dict[str, str] = {}
    brand_colors: str = ""
    max_title_chars: int = 200


class AdminCfg(BaseModel):
    max_upload_mb: int = 15
    publish_delete_window_hours: int = 48
    alert_cooldown_minutes: int = 15
    conversation_timeout_seconds: int = 300


class RetentionCfg(BaseModel):
    raw_images_days: int = 30
    artifacts_days: int = 7
    backups_days: int = 14


class GeminiCfg(BaseModel):
    enabled_in_phase: int = 2
    max_images_per_day: int = 15
    timeout_seconds: int = 180
    restart_every_images: int = 20
    max_text_per_day: int = 30
    delete_chat_after: bool = False
    model_mode: str = "default"


class CommonsCfg(BaseModel):
    enabled: bool = True
    api: str = "https://commons.wikimedia.org/w/api.php"
    max_results: int = 12


class LoggingCfg(BaseModel):
    max_mb: int = 5
    backups: int = 5


class Config(BaseModel):
    version: str = VERSION
    channel: ChannelCfg = ChannelCfg()
    sheet: SheetCfg = SheetCfg()
    topics: TopicsCfg = TopicsCfg()
    caption: CaptionCfg = CaptionCfg()
    policy: PolicyCfg = PolicyCfg()
    sources: SourcesCfg = SourcesCfg()
    images: ImagesCfg = ImagesCfg()
    image_prompt: ImagePromptCfg = ImagePromptCfg()
    admin: AdminCfg = AdminCfg()
    retention: RetentionCfg = RetentionCfg()
    gemini: GeminiCfg = GeminiCfg()
    commons: CommonsCfg = CommonsCfg()
    logging: LoggingCfg = LoggingCfg()


@dataclass(frozen=True)
class Env:
    telegram_token: str
    admin_user_id: int
    channel_id: str
    sheet_id: str
    service_account_file: Path
    chrome_path: str
    proxy_url: str
    browser_proxy: str

    @property
    def admin_ok(self) -> bool:
        return self.admin_user_id > 0


def load_env(path: Path = ENV_PATH) -> Env:
    values = dict(dotenv_values(path)) if path.exists() else {}
    # real environment variables win over the file (useful for Docker)
    def get(key: str, default: str = "") -> str:
        return (os.environ.get(key) or values.get(key) or default).strip()

    raw_admin = get("ADMIN_USER_ID", "0")
    try:
        admin_id = int(raw_admin)
    except ValueError:
        admin_id = 0
    sa = get("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/service_account.json")
    sa_path = Path(sa)
    if not sa_path.is_absolute():
        sa_path = ROOT / sa_path
    return Env(
        telegram_token=get("TELEGRAM_BOT_TOKEN"),
        admin_user_id=admin_id,
        channel_id=get("CHANNEL_ID", "@waltest0001"),
        sheet_id=get("GOOGLE_SHEET_ID"),
        service_account_file=sa_path,
        chrome_path=get("CHROME_PATH"),
        proxy_url=get("PROXY_URL"),
        browser_proxy=get("BROWSER_PROXY"),
    )


def load_config(path: Path = CONFIG_PATH) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config.model_validate(data)


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, IMAGES_DIR, ARTIFACTS_DIR, BACKUPS_DIR, CACHE_DIR, SHEET_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)
