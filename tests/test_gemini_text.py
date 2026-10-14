"""generate_text: the answer must come from Gemini, never our own echoed prompt (fake page, no Chrome)."""
from types import SimpleNamespace

import pytest

from app import gemini
from app.gemini import GeminiError, Selectors

SEL = {"prompt_box": "PB", "send_button": "SEND", "image_result": "IMG", "response_text": "RESP"}
PROMPT = "یک پست بنویس\nTITLE: <عنوان>"


class Loc:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        if self.kind == "RESP":
            return len(self.page.msgs)
        return 1 if self.kind == "PB" else 0

    def inner_text(self):
        if self.kind == "PB":
            return self.page.typed
        return self.page.msgs[-1] if self.kind == "RESP" and self.page.msgs else ""

    def click(self):
        if self.kind == "SEND":
            self.page.sent = self.page.typed
            self.page.msgs.append(self.page.typed)  # the user's own message shows up first
            self.page.typed = ""                    # the box is cleared after sending
            self.page.polls = 0

    def fill(self, text):
        self.page.typed = text


class TextPage:
    def __init__(self, answer_after_polls=3):
        self.url = gemini.GEMINI_URL
        self.msgs, self.typed, self.sent, self.polls = [], "", "", 0
        self.answer_after = answer_after_polls

    def locator(self, css):
        return Loc(self, css)

    def goto(self, url):
        self.url = url

    def wait_for_timeout(self, ms):
        self.polls += 1
        if self.msgs and self.msgs[-1] == self.sent and self.polls >= self.answer_after:
            self.msgs.append("ANSWER: این پاسخ گمینی است")


def _setup(monkeypatch, page):
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: Selectors("verified", dict(SEL)))
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    monkeypatch.setattr(gemini, "_connect", lambda port: (SimpleNamespace(stop=lambda: None), browser))


def test_answer_is_read_not_the_echoed_prompt(monkeypatch):
    page = TextPage(answer_after_polls=3)
    _setup(monkeypatch, page)
    out = gemini.generate_text(PROMPT, timeout_s=30, busy_poll_s=0.01)
    assert out == "ANSWER: این پاسخ گمینی است"


def test_never_returns_prompt_when_no_answer_arrives(monkeypatch):
    page = TextPage(answer_after_polls=10**9)  # Gemini never answers
    _setup(monkeypatch, page)
    with pytest.raises(GeminiError):
        gemini.generate_text(PROMPT, timeout_s=0, busy_poll_s=0.01)


def test_new_chat_is_opened_in_the_same_tab(monkeypatch):
    page = TextPage(answer_after_polls=2)
    navigations = []
    page.goto = lambda url: navigations.append(url)
    _setup(monkeypatch, page)
    gemini.generate_text(PROMPT, timeout_s=30, busy_poll_s=0.01)
    assert navigations == [gemini.GEMINI_URL]  # navigates inside the same tab


def test_existing_chat_can_be_used_without_navigating(monkeypatch):
    page = TextPage(answer_after_polls=2)
    navigations = []
    page.goto = lambda url: navigations.append(url)
    _setup(monkeypatch, page)
    gemini.generate_text(PROMPT, timeout_s=30, busy_poll_s=0.01, new_chat=False)
    assert navigations == []


def test_unsent_prompt_is_reported(monkeypatch):
    page = TextPage(answer_after_polls=2)
    page.locator = lambda css: Loc(page, css)

    class Stuck(Loc):
        def click(self):
            return None  # the send click does nothing; the box keeps the text
    page.locator = lambda css: Stuck(page, css) if css == "SEND" else Loc(page, css)
    _setup(monkeypatch, page)
    with pytest.raises(GeminiError, match="ارسال نشد"):
        gemini.generate_text(PROMPT, timeout_s=30, busy_poll_s=0.01)
