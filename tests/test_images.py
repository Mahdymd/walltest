import random
from pathlib import Path

import pytest
from PIL import Image

from app.config import LOGO_PATH, load_config
from app.images import ImageError, check_logo, dhash, hamming, process_image


@pytest.fixture
def photo(tmp_path):
    rnd = random.Random(1)
    img = Image.new("RGB", (1600, 900))
    px = img.load()
    for x in range(0, 1600, 4):
        for y in range(0, 900, 4):
            c = (rnd.randint(0, 255), (x * 255) // 1600, (y * 255) // 900)
            for dx in range(4):
                for dy in range(4):
                    px[x + dx, y + dy] = c
    p = tmp_path / "in.jpg"
    img.save(p, "JPEG")
    return p


def test_output_is_1280x720_jpeg_under_1mb(photo, tmp_path):
    cfg = load_config()
    out = process_image(photo, tmp_path / "out.jpg", LOGO_PATH, cfg.images)
    with Image.open(out.path) as im:
        assert im.size == (1280, 720) and im.format == "JPEG"
    assert out.size_bytes < 1_000_000
    assert len(out.dhash) == 16


def test_logo_changes_bottom_right_only(photo, tmp_path):
    cfg = load_config()
    out = process_image(photo, tmp_path / "out.jpg", LOGO_PATH, cfg.images)
    base = Image.open(photo).convert("RGB")
    from app.images import cover_crop
    plain = cover_crop(base, 1280, 720)
    with Image.open(out.path) as im:
        im = im.convert("RGB")
        diff_br = sum(1 for x in range(1100, 1270, 10) for y in range(560, 700, 10) if im.getpixel((x, y)) != plain.getpixel((x, y)))
        diff_tl = sum(1 for x in range(0, 200, 10) for y in range(0, 100, 10) if im.getpixel((x, y)) != plain.getpixel((x, y)))
    assert diff_br > 0
    assert diff_tl < diff_br


def test_blank_image_rejected(tmp_path):
    p = tmp_path / "blank.png"
    Image.new("RGB", (1000, 800), (120, 120, 120)).save(p)
    cfg = load_config()
    with pytest.raises(ImageError):
        process_image(p, tmp_path / "x.jpg", LOGO_PATH, cfg.images)


def test_small_image_rejected(tmp_path):
    p = tmp_path / "small.png"
    img = Image.effect_noise((300, 200), 60).convert("RGB")
    img.save(p)
    cfg = load_config()
    with pytest.raises(ImageError):
        process_image(p, tmp_path / "x.jpg", LOGO_PATH, cfg.images)


def test_duplicate_detection_same_image(photo, tmp_path):
    cfg = load_config()
    a = process_image(photo, tmp_path / "a.jpg", LOGO_PATH, cfg.images)
    b = process_image(photo, tmp_path / "b.jpg", LOGO_PATH, cfg.images)
    assert hamming(a.dhash, b.dhash) <= cfg.images.duplicate_hamming


def test_dhash_differs_for_different_images():
    a = Image.effect_noise((200, 200), 50).convert("RGB")
    b = Image.new("RGB", (200, 200), (0, 0, 0)).convert("RGB")
    assert hamming(dhash(a), dhash(b)) > 5


def test_logo_file_is_transparent():
    assert check_logo(LOGO_PATH) is None


def test_opaque_logo_fails_check(tmp_path):
    p = tmp_path / "logo.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(p)
    assert "شفاف" in (check_logo(p) or "")
    assert "پیدا نشد" in (check_logo(tmp_path / "missing.png") or "")
    _ = Path
