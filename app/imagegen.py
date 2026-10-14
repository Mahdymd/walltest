"""Gemini image generation for the pipeline and /testimage.

Two halves, so the SQLite store is only touched on the main thread:
- prepare_attempt(ctx): main thread. Gate, daily budget, restart counter.
- generate_blocking(...): worker thread. Only the browser work (no store access).
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import gemini, geministate
from .config import Config
from .sheets import clean
from .textutil import strip_html

log = logging.getLogger(__name__)

TEST_PROMPT = ("A simple abstract background: smooth turquoise gradient with one soft navy circle. "
               "No text, no letters, no numbers, no logos, no watermarks.")


class ImageGenError(Exception):
    """status is the sheet status to use: Waiting (retry later) or Failed. message is Persian."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def build_prompt(cfg: Config, values: dict[str, str]) -> str:
    custom = clean(values.get("ImagePrompt", ""))
    if custom:
        return custom
    pc = cfg.image_prompt
    title = clean(values.get("Title", "")) or strip_html(clean(values.get("FullText", "")))[:80]
    title = title[: pc.max_title_chars]
    style_key = clean(values.get("ImageStyle", "")).lower() or "photo"
    fragment = pc.style_fragments.get(style_key) or pc.style_fragments.get("photo", "")
    return pc.template.format(style_fragment=fragment, title=title, brand_colors=pc.brand_colors)


def prepare_attempt(ctx) -> bool:  # noqa: ANN001
    """Main thread. Raises ImageGenError when generation must not start. Returns new_chat."""
    try:
        gemini.require_verified(gemini.load_selectors())
    except gemini.GeminiError as exc:
        raise ImageGenError("Waiting", str(exc)) from exc
    g = ctx.cfg.gemini
    if geministate.daily_left(ctx.store, g.max_images_per_day) <= 0:
        raise ImageGenError(
            "Waiting",
            f"سقف روزانه‌ی Gemini ({g.max_images_per_day} تصویر) پر شده است. فردا دوباره Pending کنید یا عکس را دستی آپلود کنید.")
    return geministate.begin_attempt(ctx.store, g.restart_every_images)


def generate_blocking(prompt: str, out_dir: Path, timeout_s: int, new_chat: bool) -> Path:
    """Worker thread. Returns the raw PNG path, or raises ImageGenError."""
    try:
        return gemini.generate_image(prompt, out_dir, timeout_s=timeout_s, new_chat=new_chat)
    except gemini.GeminiError as exc:
        log.warning("gemini generation stopped: %s", exc)
        raise ImageGenError("Waiting", str(exc)) from exc
