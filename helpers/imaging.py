"""pure pil crop helpers for the guessing game (import-light so they can run off-thread)."""

import io
import random

from PIL import Image


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _out(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# true display aspect (w/h) per squished-into-pot asset; see the frontend CardMedia/presetImage
ASPECT_FULL = 16 / 9  # card full art (2048x1024)
ASPECT_LOGO = 16 / 10  # event logo (1024x512)
ASPECT_BANNER = 16 / 9  # event banner (2048x1024)


def unsquish(data: bytes, aspect: float) -> bytes:
    """shrink the over-long axis of a squished pot texture to its true aspect (no upscale)."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = img.size
    src = w / h
    if src > aspect:
        img = img.resize((max(1, round(h * aspect)), h), Image.Resampling.LANCZOS)
    elif src < aspect:
        img = img.resize((w, max(1, round(w / aspect))), Image.Resampling.LANCZOS)
    return _out(img)


def crop_square(
    data: bytes, *, frac: float = 0.42, grayscale: bool = False, seed: int | None = None
) -> bytes:
    img = _open(data)
    w, h = img.size
    side = max(8, int(min(w, h) * frac))
    rng = random.Random(seed)
    x = rng.randint(0, max(0, w - side))
    y = rng.randint(0, max(0, h - side))
    crop = img.crop((x, y, x + side, y + side))
    if grayscale:
        crop = crop.convert("L").convert("RGB")
    if side < 320:
        crop = crop.resize((320, 320), Image.Resampling.LANCZOS)
    return _out(crop)


def pixelate(data: bytes, *, px: int = 30, out: int = 320) -> bytes:
    img = _open(data).resize((px, px), Image.Resampling.BILINEAR)
    return _out(img.resize((out, out), Image.Resampling.NEAREST))


def mirror(data: bytes) -> bytes:
    return _out(_open(data).transpose(Image.Transpose.FLIP_LEFT_RIGHT))


def crop_chart_strip(data: bytes, *, frac: float = 0.16, seed: int | None = None) -> bytes:
    img = _open(data)
    w, h = img.size
    strip = max(8, int(h * frac))
    rng = random.Random(seed)
    y = rng.randint(0, max(0, h - strip))
    return _out(img.crop((0, y, w, y + strip)))
