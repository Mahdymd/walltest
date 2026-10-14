"""Gemini image URLs: request the original size, and keep small preview URLs untouched elsewhere."""
from types import SimpleNamespace

import pytest

from app import gemini, images


@pytest.mark.parametrize("src, expected", [
    ("https://lh3.googleusercontent.com/abc123=s512-rj", "https://lh3.googleusercontent.com/abc123=s0"),
    ("https://lh3.googleusercontent.com/abc123=w1024-h768-n", "https://lh3.googleusercontent.com/abc123=s0"),
    ("https://lh3.googleusercontent.com/abc123", "https://lh3.googleusercontent.com/abc123"),
    ("https://example.com/pic.png?size=512", "https://example.com/pic.png?size=512"),
    ("https://evil.com/x=s512", "https://evil.com/x=s512"),
])
def test_original_image_url(src, expected):
    assert gemini.original_image_url(src) == expected


def test_image_bytes_downloads_the_original(monkeypatch):
    asked = []

    class Req:
        def get(self, url):
            asked.append(url)
            return SimpleNamespace(body=lambda: b"PNG")
    page = SimpleNamespace(request=Req())
    assert gemini._image_bytes(page, "https://lh3.googleusercontent.com/z=s512-rj") == b"PNG"
    assert asked == ["https://lh3.googleusercontent.com/z=s0"]


def test_small_image_error_shows_real_size(tmp_path):
    from PIL import Image
    path = tmp_path / "small.png"
    Image.new("RGB", (512, 512), (10, 200, 30)).save(path)
    with pytest.raises(images.ImageError, match="512×512"):
        images.open_validated(path, 768)


def test_blob_fetch_error_falls_back_instead_of_failing():
    class Boom:
        def evaluate(self, js, arg=None):
            raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
    assert gemini._image_bytes(Boom(), "blob:https://gemini.google.com/abc") is None


def test_canvas_failure_returns_none():
    class NoCanvas:
        def evaluate(self, js, arg=None):
            raise RuntimeError("tainted canvas")
    assert gemini._canvas_bytes(NoCanvas()) is None
