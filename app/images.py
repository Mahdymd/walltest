"""Image processing with Pillow: 16:9 cover crop, logo watermark, validation, perceptual hash.

Pure classic image algorithms (dHash). No ML.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from .config import ImagesCfg


class ImageError(Exception):
    """Message is Persian and shown to the admin as-is."""


@dataclass
class Processed:
    path: Path
    dhash: str
    size_bytes: int
    logo_panel: bool


def open_validated(path: Path, min_side: int) -> Image.Image:
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ImageError("فایل تصویر خوانده نشد. یک عکس معمولی (JPG یا PNG) بفرستید.") from exc
    if min(img.size) < min_side:
        w, h = img.size
        raise ImageError(f"تصویر خیلی کوچک است ({w}×{h}). کوچک‌ترین ضلع باید {min_side} پیکسل یا بیشتر باشد.")
    gray = img.convert("L")
    stddev = ImageStat.Stat(gray).stddev[0]
    if stddev < 6:
        raise ImageError("تصویر خالی یا تک‌رنگ است. عکس دیگری بفرستید.")
    return img


def cover_crop(img: Image.Image, width: int, height: int) -> Image.Image:
    src = img.convert("RGB")
    scale = max(width / src.width, height / src.height)
    new_size = (round(src.width * scale), round(src.height * scale))
    resized = src.resize(new_size, Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def dhash(img: Image.Image) -> str:
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    # get_flattened_data is the Pillow 12+ API; getdata is deprecated there.
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _logo_layer(logo_path: Path, canvas_w: int, cfg: ImagesCfg) -> Image.Image:
    lg = cfg.logo
    logo = Image.open(logo_path).convert("RGBA")
    target_w = max(1, round(canvas_w * lg.width_ratio))
    target_h = max(1, round(logo.height * target_w / logo.width))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)
    alpha = logo.getchannel("A").point(lambda a: int(a * lg.opacity))
    logo.putalpha(alpha)
    return logo


def add_logo(base: Image.Image, logo_path: Path, cfg: ImagesCfg) -> tuple[Image.Image, bool]:
    lg = cfg.logo
    canvas = base.convert("RGBA")
    logo = _logo_layer(logo_path, canvas.width, cfg)
    x = canvas.width - logo.width - lg.margin
    y = canvas.height - logo.height - lg.margin
    panel_used = False
    if lg.auto_contrast:
        region = canvas.crop((x, y, x + logo.width, y + logo.height)).convert("L")
        bg_mean = ImageStat.Stat(region).mean[0]
        logo_luma = ImageStat.Stat(logo.convert("L"), mask=logo.getchannel("A")).mean[0]
        if abs(bg_mean - logo_luma) < lg.contrast_min:
            pad = 14
            panel = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(panel).rounded_rectangle(
                (x - pad, y - pad, x + logo.width + pad, y + logo.height + pad),
                radius=18, fill=(0, 0, 0, 110))
            canvas = Image.alpha_composite(canvas, panel)
            panel_used = True
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(logo, (x, y), logo)
    return Image.alpha_composite(canvas, layer).convert("RGB"), panel_used


def process_image(src: Path, out: Path, logo_path: Path, cfg: ImagesCfg) -> Processed:
    img = open_validated(src, cfg.min_side)
    cropped = cover_crop(img, cfg.width, cfg.height)
    final, panel = add_logo(cropped, logo_path, cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    quality = cfg.jpeg_quality
    final.save(out, "JPEG", quality=quality, optimize=True)
    while out.stat().st_size > cfg.max_bytes and quality > 70:
        quality -= 5
        final.save(out, "JPEG", quality=quality, optimize=True)
    return Processed(path=out, dhash=dhash(final), size_bytes=out.stat().st_size, logo_panel=panel)


def check_logo(logo_path: Path) -> str | None:
    """Return a Persian error message if the logo is missing or not transparent."""
    if not logo_path.exists():
        return "فایل assets/logo.png پیدا نشد. لوگوی کانال را با همین نام در پوشه assets بگذارید."
    try:
        img = Image.open(logo_path)
    except Exception:  # noqa: BLE001
        return "فایل assets/logo.png خوانده نشد."
    if img.mode not in ("RGBA", "LA") and not (img.mode == "P" and "transparency" in img.info):
        return "لوگو شفاف نیست. لوگو باید PNG با پس‌زمینه‌ی شفاف (کانال آلفا) باشد."
    alpha = img.convert("RGBA").getchannel("A")
    if alpha.getextrema()[0] == 255:
        return "لوگو شفاف نیست (هیچ بخش شفافی ندارد). فایل را با پس‌زمینه‌ی شفاف ذخیره کنید."
    return None
