import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Env, load_config  # noqa: E402
from app.context import build_context, reload_rules  # noqa: E402
from app.sheets import COLUMNS, MemoryBackend  # noqa: E402
from app.store import Store  # noqa: E402


def make_row(**vals) -> list[str]:
    return [str(vals.get(c, "")) for c in COLUMNS]


class Recorder:
    """Fake notifier: records what the admin would have seen."""

    def __init__(self):
        self.events: list[tuple] = []

    async def progress(self, post_id, stage, note=""):
        self.events.append(("progress", post_id, stage))

    async def draft_ready(self, post_id):
        self.events.append(("draft_ready", post_id))

    async def blocked(self, post_id, report):
        self.events.append(("blocked", post_id))

    async def stale(self, post_id, version, reason):
        self.events.append(("stale", post_id, version, reason))

    async def alert(self, text):
        self.events.append(("alert", text))

    def kinds(self):
        return [e[0] for e in self.events]


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.calls = 0

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, chat_id, photo, caption="", **kw):
        self.sent.append({"chat_id": chat_id, "photo": photo, "caption": caption, **kw})
        return SimpleNamespace(message_id=len(self.sent), photo=[SimpleNamespace(file_id="FILE-1")])

    async def get_chat(self, chat_id):
        return SimpleNamespace(username="waltest0001")

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.sent.append({"edited": message_id, "chat_id": chat_id, "text": text})
        return True

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.sent.append({"markup_removed": message_id, "chat_id": chat_id})
        return True

    async def edit_message_caption(self, chat_id, message_id, caption, **kw):
        self.sent.append({"caption_edited": message_id, "chat_id": chat_id, "caption": caption})
        return True

    async def delete_message(self, chat_id, message_id):
        self.sent.append({"deleted": message_id, "chat_id": chat_id})
        return True


@pytest.fixture(autouse=True)
def no_real_chrome(monkeypatch):
    """Tests never reach a real Chrome. Tests that need the browser monkeypatch _connect themselves."""
    from app import gemini

    def refuse(port):  # noqa: ANN001
        raise gemini.GeminiError("Chrome در دسترس نیست (تست)")

    monkeypatch.setattr(gemini, "_connect", refuse)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def backend():
    return MemoryBackend({
        "Content": [list(COLUMNS)],
        "BannedWords": [["Word", "Level", "Note"], ["کلاهبرداری", "hard", ""], ["بدون ریسک", "soft", ""]],
        "Sources": [["Domain"], ["en.wikipedia.org"]],
    })


@pytest.fixture
def env(tmp_path):
    return Env(telegram_token="123:TEST", admin_user_id=111, channel_id="@waltest0001", sheet_id="sheet",
               service_account_file=tmp_path / "sa.json", chrome_path="", proxy_url="", browser_proxy="")


@pytest.fixture
def ctx(cfg, store, backend, env):
    c = build_context(cfg, env, store, backend=backend)
    reload_rules(c)
    return c


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def fake_bot():
    return FakeBot()
