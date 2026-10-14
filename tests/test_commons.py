"""Wikimedia Commons lookup: license rules, filtering, download limits. Network is mocked."""
import httpx
import pytest

from app import commons
from app.commons import CommonsError

API = "https://commons.wikimedia.org/w/api.php"


def page(idx, license_name, mime="image/jpeg", w=1200, h=900, artist="<a>Ali</a>"):
    return {"index": idx, "imageinfo": [{
        "mime": mime, "width": w, "height": h,
        "url": f"https://upload.wikimedia.org/wikipedia/commons/x{idx}.jpg",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:x{idx}.jpg",
        "extmetadata": {"LicenseShortName": {"value": license_name}, "Artist": {"value": artist}},
    }]}


def api(pages):
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"query": {"pages": pages}}))


@pytest.mark.parametrize("name, allowed", [
    ("CC0", True), ("Public domain", True), ("CC BY 4.0", True), ("CC BY-SA 4.0", True),
    ("CC BY-NC-SA 2.0", False), ("CC BY-ND 3.0", False), ("All rights reserved", False), ("", False),
])
def test_license_rules(name, allowed):
    assert commons.license_allowed(name) is allowed


def test_first_allowed_photo_is_picked_in_search_order():
    pages = [page(2, "CC BY-NC-SA 2.0"), page(1, "CC BY-SA 4.0", w=400, h=300),
             page(3, "CC BY-SA 4.0", artist="<a>Sara</a>"), page(4, "CC0")]
    hit = commons.search("solar", api=API, min_side=768, max_results=10, transport=api(pages))
    assert hit is not None
    assert hit.page_url.endswith("x3.jpg") and hit.license == "CC BY-SA 4.0"
    assert hit.author == "Sara"


def test_no_allowed_photo_returns_none():
    hit = commons.search("solar", api=API, min_side=768, max_results=10,
                         transport=api([page(1, "CC BY-NC 4.0"), page(2, "CC BY", mime="image/gif")]))
    assert hit is None


def test_empty_terms_make_no_request():
    def boom(req):  # noqa: ANN001
        raise AssertionError("no request expected")
    assert commons.search("   ", api=API, min_side=768, max_results=10,
                          transport=httpx.MockTransport(boom)) is None


def test_download_writes_file(tmp_path):
    hit = commons.Hit(page_url="https://commons.wikimedia.org/wiki/File:a.jpg",
                      image_url="https://upload.wikimedia.org/a.jpg", width=1000, height=800,
                      license="CC0", author="x")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"JPEGDATA"))
    out = commons.download(hit, tmp_path / "c.jpg", max_bytes=1000, transport=transport)
    assert out.read_bytes() == b"JPEGDATA"


def test_download_respects_size_limit(tmp_path):
    hit = commons.Hit("https://commons.wikimedia.org/wiki/File:a.jpg", "https://upload.wikimedia.org/a.jpg",
                      1000, 800, "CC0", "x")
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(CommonsError):
        commons.download(hit, tmp_path / "c.jpg", max_bytes=1000, transport=transport)
    assert not (tmp_path / "c.jpg").exists()


def test_network_error_means_nothing_found(tmp_path, monkeypatch):
    def down(*a, **k):  # noqa: ANN002, ANN003
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(commons, "search", down)
    assert commons.find_photo("solar", tmp_path, api=API, min_side=768, max_results=5,
                              max_bytes=10_000) is None
