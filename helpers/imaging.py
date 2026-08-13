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
    data: bytes, *, size: int = 250, grayscale: bool = False, seed: int | None = None
) -> bytes:
    # a random size x size window at native resolution, no upscaling (1:1 with sbuga _crop_square)
    img = _open(data)
    if grayscale:
        img = img.convert("L")
    w, h = img.size
    s = min(size, w, h)
    rng = random.Random(seed)
    x = rng.randint(0, w - s)
    y = rng.randint(0, h - s)
    return _out(img.crop((x, y, x + s, y + s)))


def mirror(data: bytes) -> bytes:
    return _out(_open(data).transpose(Image.Transpose.FLIP_LEFT_RIGHT))


def crop_chart_strip(data: bytes, *, frac: float = 0.16, seed: int | None = None) -> bytes:
    img = _open(data)
    w, h = img.size
    strip = max(8, int(h * frac))
    rng = random.Random(seed)
    y = rng.randint(0, max(0, h - strip))
    return _out(img.crop((0, y, w, y + strip)))
