"""pure pil crop helpers for the guessing game (import-light so they can run off-thread)."""

import io
import random

import numpy as np
from PIL import Image


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _out(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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


def mirror_chart(data: bytes) -> bytes:
    # mirror the note lanes within each time column in place (a full-image flip would reverse the
    # column/time order and the footer). same 80px margin / 272px pitch / 192px column geometry as
    # the crop; the 32px top and 287px footer bands are left untouched.
    arr = np.asarray(_open(data), dtype=np.uint8).copy()
    height, width, _ = arr.shape
    cols = max(1, round((width - 80) / 272))
    y0, y1 = 32, height - 287
    for i in range(cols):
        x0 = 80 + 272 * i
        x1 = min(width, x0 + 192)
        arr[y0:y1, x0:x1] = arr[y0:y1, x0:x1, :][:, ::-1]
    out = io.BytesIO()
    Image.fromarray(arr).save(out, "PNG")
    return out.getvalue()


def crop_chart(data: bytes) -> bytes:
    # one full-height note column (80px left margin, 272px pitch, 192px content; 32px top / 287px
    # footer margins), split in half and laid side-by-side so the tall column reads squarer. the
    # chart png is the same format/dimensions as pjsk's.
    arr = np.asarray(_open(data), dtype=np.uint8)
    height, width, _ = arr.shape
    cols = max(3, round((width - 80) / 272))
    col = random.randint(2, cols - 1)
    start_x = 80 + 272 * (col - 1)
    cropped = arr[32 : height - 287, start_x : start_x + 192]
    mid_y = cropped.shape[0] // 2
    img1, img2 = cropped[: mid_y + 20], cropped[mid_y - 20 :]
    final_height = max(img1.shape[0], img2.shape[0])
    final = np.full((final_height, 410, 3), 255, dtype=np.uint8)
    final[: img2.shape[0], 10 : 10 + img2.shape[1]] = img2
    final[: img1.shape[0], 210 : 210 + img1.shape[1]] = img1
    out = io.BytesIO()
    Image.fromarray(final).save(out, "PNG")
    return out.getvalue()
