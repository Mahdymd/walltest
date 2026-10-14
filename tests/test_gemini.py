"""Phase 2 browser layer, tested with fakes only (no Chrome, no Gemini account)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import gemini
from app.gemini import (GeminiError, Selectors, assert_on_gemini, generate_image, is_gemini_url,
                        login_command, require_verified)

VERIFIED = Selectors(status="verified", elements={
    "prompt_box": "div.prompt", "send_button": "button.send",
    "image_result": "img.result", "busy_indicator": "div.busy",
})


# ---------- URL guard ----------

@pytest.mark.parametrize("url", [
    "https://gemini.google.com/app",
    "https://gemini.google.com/app/abc123",
    "https://GEMINI.google.com/app",
])
def test_gemini_urls_allowed(url):
    assert is_gemini_url(url)


@pytest.mark.parametrize("url", [
    "http://gemini.google.com/app",              # not https
    "https://gemini.google.com.evil.com/app",    # suffix trick
    "https://evil.com/?q=gemini.google.com",     # query trick
    "https://accounts.google.com/signin",        # login redirect is NOT allowed
    "about:blank",
    "",
    "not a url",
])
def test_other_urls_refused(url):
    assert not is_gemini_url(url)


def test_assert_on_gemini_raises_with_persian_message():
    with pytest.raises(GeminiError, match="خارج از Gemini"):
        assert_on_gemini("https://evil.com/")


# ---------- selector gate ----------

def test_unverified_selectors_are_refused():
    sel = Selectors(status="needs_verification", elements=dict(VERIFIED.elements))
    with pytest.raises(GeminiError, match="تأیید نشده"):
        require_verified(sel)


def test_verified_but_empty_required_element_is_refused():
    sel = Selectors(status="verified", elements={"prompt_box": "x"})
    with pytest.raises(GeminiError):
        require_verified(sel)


def test_shipped_selectors_file_is_consistent():
    """A file marked verified must carry the three required selectors (the gate depends on it)."""
    sel = gemini.load_selectors()
    if sel.verified:
        require_verified(sel)  # does not raise
    else:
        with pytest.raises(GeminiError):
            require_verified(sel)


def test_login_command_has_port_profile_and_gemini_url(tmp_path):
    cmd = login_command("C:/chrome.exe", port=9333, profile=tmp_path / "p")
    assert cmd[0] == "C:/chrome.exe"
    assert "--remote-debugging-port=9333" in cmd
    assert any(a.startswith("--user-data-dir=") for a in cmd)
    assert cmd[-1] == gemini.GEMINI_URL


# ---------- fake browser ----------

class FakeLoc:
    def __init__(self, page, css):
        self.page, self.css = page, css

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return self.page.counts.get(self.css, 0)

    def nth(self, i):
        return self

    def evaluate_all(self, js, arg=None):
        # the fake images are all large and loaded; icons are not modelled here
        return list(range(self.count()))

    def click(self):
        self.page.log.append(("click", self.css))
        if self.css == VERIFIED.elements["send_button"] and self.page.produce_image:
            self.page.counts[VERIFIED.elements["image_result"]] = self.page.counts.get(
                VERIFIED.elements["image_result"], 0) + 1

    def fill(self, text):
        self.page.log.append(("fill", self.css, text))
        if self.page.navigate_on_fill:
            self.page.url = "https://accounts.google.com/signin"

    def get_attribute(self, name):
        return self.page.src if name == "src" else None

    def screenshot(self, path):
        Path(path).write_bytes(b"SHOT")


class FakePage:
    def __init__(self, url=f"https://{gemini.GEMINI_HOST}/app", produce_image=True, navigate_on_fill=False,
                 src="https://lh3.example.googleusercontent.com/abc", goto_lands_on=None):
        self.url = url
        self.goto_lands_on = goto_lands_on
        self.counts = {VERIFIED.elements["image_result"]: 0}
        self.counts[VERIFIED.elements["prompt_box"]] = 1  # the input box is on the page
        self.produce_image = produce_image
        self.navigate_on_fill = navigate_on_fill
        self.src = src
        self.log = []
        self.request = SimpleNamespace(get=lambda u: SimpleNamespace(body=lambda: b"PNGDATA"))

    def locator(self, css):
        return FakeLoc(self, css)

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))

    def goto(self, url):
        self.log.append(("goto", url))
        self.url = self.goto_lands_on or url

    def evaluate(self, js, arg=None):
        self.log.append(("evaluate", arg))
        return None  # blob fetch unavailable in the fake: callers fall back to a screenshot


def _fake_connect(page):
    context = SimpleNamespace(pages=[page])
    browser = SimpleNamespace(contexts=[context])
    pw = SimpleNamespace(stop=lambda: None)
    return lambda port: (pw, browser)


def test_generate_refuses_when_not_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: Selectors("needs_verification", dict(VERIFIED.elements)))
    with pytest.raises(GeminiError):
        generate_image("x", tmp_path)


def test_generate_saves_new_image(tmp_path, monkeypatch):
    page = FakePage()
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    out = generate_image("a calm sea", tmp_path, timeout_s=5, busy_poll_s=0)
    assert out.exists() and out.read_bytes() == b"PNGDATA"
    assert ("fill", VERIFIED.elements["prompt_box"], "a calm sea") in page.log
    assert ("click", VERIFIED.elements["send_button"]) in page.log


def test_generate_falls_back_to_screenshot_for_blob_src(tmp_path, monkeypatch):
    page = FakePage(src="blob:https://gemini.google.com/123")
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    out = generate_image("p", tmp_path, timeout_s=5, busy_poll_s=0)
    assert out.read_bytes() == b"SHOT"


def test_navigation_away_stops_generation(tmp_path, monkeypatch):
    page = FakePage(navigate_on_fill=True)
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    with pytest.raises(GeminiError, match="خارج از Gemini"):
        generate_image("p", tmp_path, timeout_s=5, busy_poll_s=0)
    # nothing was sent after leaving Gemini
    assert ("click", VERIFIED.elements["send_button"]) not in page.log


def test_timeout_when_no_image_appears(tmp_path, monkeypatch):
    page = FakePage(produce_image=False)
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    with pytest.raises(GeminiError, match="مهلت"):
        generate_image("p", tmp_path, timeout_s=0, busy_poll_s=0)


def test_missing_gemini_tab_is_reported(monkeypatch):
    page = FakePage(url="https://example.com/")
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    with pytest.raises(GeminiError, match="صفحه‌ی Gemini"):
        gemini._gemini_page(browser)


def test_new_chat_navigates_to_gemini_before_typing(tmp_path, monkeypatch):
    page = FakePage()
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    generate_image("p", tmp_path, timeout_s=5, busy_poll_s=0, new_chat=True)
    goto_at = page.log.index(("goto", gemini.GEMINI_URL))
    fill_at = page.log.index(("fill", VERIFIED.elements["prompt_box"], "p"))
    assert goto_at < fill_at


def test_new_chat_refused_when_redirected_off_gemini(tmp_path, monkeypatch):
    page = FakePage(goto_lands_on="https://accounts.google.com/signin")
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    with pytest.raises(GeminiError, match="خارج از Gemini"):
        generate_image("p", tmp_path, timeout_s=5, busy_poll_s=0, new_chat=True)
    assert ("fill", VERIFIED.elements["prompt_box"], "p") not in page.log


def test_https_image_is_downloaded_with_page_request(tmp_path, monkeypatch):
    page = FakePage(src="https://lh3.example.googleusercontent.com/abc")
    monkeypatch.setattr(gemini, "load_selectors", lambda *a, **k: VERIFIED)
    monkeypatch.setattr(gemini, "_connect", _fake_connect(page))
    out = generate_image("p", tmp_path, timeout_s=5, busy_poll_s=0)
    assert out.read_bytes() == b"PNGDATA"
