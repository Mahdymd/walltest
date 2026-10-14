"""Discovery output for --inspect: technical attributes only, never message text."""

from app import gemini
from app.gemini import CANDIDATE_QUERIES, print_candidates, suggest_css


def test_suggest_prefers_aria_label():
    assert suggest_css({"tag": "BUTTON", "aria": "Send message"}) == 'button[aria-label="Send message"]'


def test_suggest_contenteditable():
    assert suggest_css({"tag": "DIV", "ce": "true"}) == 'div[contenteditable="true"]'


def test_suggest_falls_back_to_tag_only():
    assert suggest_css({"tag": "TEXTAREA"}) == "textarea"


def test_suggest_handles_missing_tag():
    assert suggest_css({}) == "*"


class FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakeDiscoverPage:
    def __init__(self, counts, described):
        self.counts = counts
        self.described = described
        self.evaluated = []

    def locator(self, query):
        return FakeLocator(self.counts.get(query, 0))

    def eval_on_selector_all(self, query, js):
        self.evaluated.append((query, js))
        return self.described.get(query, [])


def test_print_candidates_shows_counts_and_suggestions(capsys):
    page = FakeDiscoverPage(
        counts={CANDIDATE_QUERIES["prompt_box"]: 1, CANDIDATE_QUERIES["send_button"]: 0,
                CANDIDATE_QUERIES["image_result"]: 4},
        described={CANDIDATE_QUERIES["prompt_box"]: [{"tag": "DIV", "ce": "true", "cls": "ql-editor"}]},
    )
    print_candidates(page)
    out = capsys.readouterr().out
    assert "prompt_box: 1 مورد" in out
    assert "send_button: 0 مورد" in out
    assert "image_result: 4 مورد" in out
    assert 'پیشنهاد: div[contenteditable="true"]' in out


def test_discovery_js_never_reads_text_content():
    # The describe script must not touch textContent/innerText/value: that could leak chat content.
    js = gemini._DESCRIBE_JS
    for forbidden in ("textContent", "innerText", "innerHTML", ".value", "alt"):
        assert forbidden not in js
