"""Red lines (section 1.1). If any of these fail, the deliverable is rejected."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
# Local runtime and secret locations (all listed in .gitignore, never shipped in the zip).
SKIP_DIRS = {".git", "data", "secrets", "logs", "chrome_profile", ".venv", "venv", "__pycache__",
             ".pytest_cache", "node_modules"}
SKIP_FILES = {".env"}
TEXT_SUFFIXES = {".py", ".txt", ".json", ".yaml", ".yml", ".md", ".bat", ".sh", ".ini", ".env",
                 ".example", ".toml", ".cfg", ""}
FORBIDDEN = [
    "openai", "anthropic", "google-generativeai", "google-genai", "vertexai", "cloudflare",
    "api.cloudflare.com", "generativelanguage.googleapis.com", "aiplatform.googleapis.com",
    "ollama", "transformers", "torch", "langchain", "litellm",
]
SEND_CALL = re.compile(r"\.(send_photo|send_message|send_media_group|send_document|send_video)\(")


def repo_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.resolve() == SELF or p.name in SKIP_FILES:
            continue
        if p.suffix.lower() in TEXT_SUFFIXES or p.name in ("Dockerfile", "requirements.txt", ".env.example"):
            yield p


def test_no_forbidden_packages_or_endpoints_anywhere():
    hits = []
    for p in repo_files():
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
        for word in FORBIDDEN:
            if re.search(r"(?<![a-z0-9_])" + re.escape(word) + r"(?![a-z0-9_])", text):
                hits.append(f"{p.relative_to(ROOT)}: {word}")
    assert not hits, "کلمات/پکیج‌های ممنوع پیدا شد:\n" + "\n".join(hits)


def test_only_publisher_sends_to_channel():
    offenders = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.parts[0] == "tests":
            continue
        if rel.as_posix() == "app/publisher.py":
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if SEND_CALL.search(line):
                window = " ".join(lines[i:i + 4]).lower()
                if "channel" in window or "chat_id=self.ctx.env.channel" in window:
                    offenders.append(f"{rel}:{i + 1}")
    assert not offenders, "ارسال به کانال خارج از publisher.py: " + ", ".join(offenders)


def test_channel_id_only_read_in_allowed_modules():
    allowed = {"app/publisher.py", "app/health.py", "app/telegram_bot.py", "app/config.py", "app/main.py"}
    offenders = []
    for p in (ROOT / "app").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if "channel_id" in p.read_text(encoding="utf-8") and rel not in allowed:
            offenders.append(rel)
    assert not offenders, f"CHANNEL_ID فقط باید در ماژول‌های مجاز باشد: {offenders}"


def test_no_publish_path_without_admin_click():
    src = (ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert "publisher" not in src and "send_to_channel" not in src and "send_message" not in src
    text = (ROOT / "app" / "publisher.py").read_text(encoding="utf-8")
    assert "schedule" not in text.lower() and "job_queue" not in text.lower()


def test_no_secrets_in_repo_files():
    token = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")
    key = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
    hits = []
    for p in repo_files():
        t = p.read_text(encoding="utf-8", errors="ignore")
        if token.search(t) or key.search(t):
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, f"راز احتمالی در فایل‌ها: {hits}"


def test_no_ai_model_calls_in_code():
    bad = re.compile(r"(generativelanguage|generate_content|chat\.completions|messages\.create|InferenceClient)")
    hits = []
    for p in (ROOT / "app").rglob("*.py"):
        if bad.search(p.read_text(encoding="utf-8")):
            hits.append(p.relative_to(ROOT).as_posix())
    assert not hits, hits
