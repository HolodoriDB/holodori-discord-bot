"""render a leaderboard table to a png (rank + change, name, score [+ change]).

no per-row card art (holodori leaderboard rows carry only rank/score/name/rankChange/epChange) and
no games-per-hour. font is resolved from a bundled ttf, then common system fonts, then pillow's
sized default so it works on windows and the linux server.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

_BG = (17, 17, 17, 255)
_ROW_ALT = (25, 25, 30, 255)
_TEXT = (242, 245, 250, 255)
_MUTED = (150, 162, 178, 255)
_UP = (120, 200, 130, 255)
_DOWN = (231, 106, 106, 255)
_YOU = (255, 205, 70, 255)

_FONT_CANDIDATES = [
    "data/assets/fonts/font.ttf",
    "data/assets/fonts/font-bold.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)  # pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


@dataclass
class LBRow:
    rank: str
    name: str
    values: list[str]
    delta_dir: int = 0  # +1 up, -1 down, 0 none
    delta_n: int = 0
    is_you: bool = False


def _len(font, text: str) -> float:
    return font.getlength(text) if hasattr(font, "getlength") else len(text) * 7


def _draw_delta(draw, x: float, mid: float, direction: int, n: int, font) -> None:
    if direction == 0:
        return
    color = _UP if direction > 0 else _DOWN
    w, h = 11, 9
    top = mid - h / 2
    if direction > 0:
        points = [(x, top + h), (x + w, top + h), (x + w / 2, top)]
    else:
        points = [(x, top), (x + w, top), (x + w / 2, top + h)]
    draw.polygon(points, fill=color)
    draw.text((x + w + 5, mid), str(n), font=font, fill=color, anchor="lm")


def render_leaderboard(rows: list[LBRow], columns: list[str], *, show_delta: bool = False) -> bytes:
    pad = 22
    row_h = 56
    rank_w = 150 if show_delta else 90
    name_w = 420
    col_w = 190
    width = pad + rank_w + name_w + col_w * len(columns) + pad
    header_h = 42
    height = header_h + row_h * max(1, len(rows)) + pad

    img = Image.new("RGBA", (width, height), _BG)
    draw = ImageDraw.Draw(img)
    f_rank = _font(24)
    f_delta = _font(18)
    f_name = _font(23)
    f_val = _font(22)
    f_head = _font(18)

    name_x = pad + rank_w
    col_x = name_x + name_w

    draw.text((pad, header_h // 2), "Rank", font=f_head, fill=_MUTED, anchor="lm")
    draw.text((name_x, header_h // 2), "Name", font=f_head, fill=_MUTED, anchor="lm")
    for i, label in enumerate(columns):
        draw.text(
            (col_x + col_w * (i + 1) - 10, header_h // 2),
            label,
            font=f_head,
            fill=_MUTED,
            anchor="rm",
        )

    for i, row in enumerate(rows):
        y = header_h + i * row_h
        mid = y + row_h // 2
        if i % 2:
            draw.rectangle([0, y, width, y + row_h], fill=_ROW_ALT)
        color = _YOU if row.is_you else _TEXT
        draw.text((pad, mid), row.rank, font=f_rank, fill=color, anchor="lm")
        if show_delta:
            _draw_delta(draw, pad + _len(f_rank, row.rank) + 10, mid, row.delta_dir, row.delta_n, f_delta)
        name = row.name
        while name and _len(f_name, name) > name_w - 14:
            name = name[:-1]
        draw.text((name_x, mid), name, font=f_name, fill=color, anchor="lm")
        for j, value in enumerate(row.values):
            draw.text(
                (col_x + col_w * (j + 1) - 10, mid),
                value,
                font=f_val,
                fill=_TEXT if value not in ("N/A", "-") else _MUTED,
                anchor="rm",
            )

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
