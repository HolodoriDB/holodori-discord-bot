"""render an event points-over-time line graph (tier cutoff and/or a followed player) to a png.

fed by /api/events/graph, whose GraphSeries has `tier` and `user` as [[ms, score], ...].
"""

from __future__ import annotations

import datetime
import io

from PIL import Image, ImageDraw

from services.leaderboard import _font

_BG = (17, 17, 17, 255)
_GRID = (44, 48, 58, 255)
_TEXT = (242, 245, 250, 255)
_MUTED = (150, 162, 178, 255)
_SS = 2  # supersample


def _fmt_score(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


def _fmt_time(ms: float, tz: datetime.tzinfo) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz).strftime("%m/%d %H:%M")


def render_graph(
    lines: list[tuple[str, list, tuple[int, int, int, int]]],
    title: str,
    *,
    tz: datetime.tzinfo | None = None,
) -> bytes:
    tz = tz or datetime.timezone.utc
    points = [(int(x), float(y)) for _, series, _ in lines for x, y in series]
    if not points:
        img = Image.new("RGBA", (600, 200), _BG)
        d = ImageDraw.Draw(img)
        d.text((20, 90), "No data.", font=_font(24), fill=_MUTED)
        out = io.BytesIO()
        img.convert("RGB").save(out, "PNG")
        return out.getvalue()

    W, H = 1000 * _SS, 560 * _SS
    ml, mr, mt, mb = 100 * _SS, 30 * _SS, 74 * _SS, 64 * _SS
    x0, x1 = ml, W - mr
    y0, y1 = mt, H - mb

    xs_min = min(p[0] for p in points)
    xs_max = max(p[0] for p in points)
    ys_max = max(p[1] for p in points) * 1.08 or 1.0
    xspan = max(1, xs_max - xs_min)

    def px(ms: float) -> float:
        return x0 + (ms - xs_min) / xspan * (x1 - x0)

    def py(score: float) -> float:
        return y1 - score / ys_max * (y1 - y0)

    img = Image.new("RGBA", (W, H), _BG)
    draw = ImageDraw.Draw(img)
    f_title = _font(26 * _SS)
    f_axis = _font(15 * _SS)
    f_leg = _font(16 * _SS)

    draw.text((ml, 24 * _SS), title, font=f_title, fill=_TEXT)

    # y grid + labels
    for i in range(5):
        y = y1 - i / 4 * (y1 - y0)
        draw.line([(x0, y), (x1, y)], fill=_GRID, width=_SS)
        draw.text((x0 - 12 * _SS, y), _fmt_score(ys_max * i / 4), font=f_axis, fill=_MUTED, anchor="rm")
    # x labels
    for i in range(6):
        ms = xs_min + i / 5 * xspan
        x = px(ms)
        draw.line([(x, y0), (x, y1)], fill=_GRID, width=_SS)
        draw.text((x, y1 + 8 * _SS), _fmt_time(ms, tz), font=f_axis, fill=_MUTED, anchor="mt")

    for name, series, color in lines:
        pts = [(px(int(x)), py(float(y))) for x, y in series]
        if len(pts) >= 2:
            draw.line(pts, fill=color, width=3 * _SS, joint="curve")
        elif pts:
            draw.ellipse(
                [pts[0][0] - 4 * _SS, pts[0][1] - 4 * _SS, pts[0][0] + 4 * _SS, pts[0][1] + 4 * _SS],
                fill=color,
            )

    # legend (top-right)
    lx, ly = x1 - 200 * _SS, 26 * _SS
    for name, _, color in lines:
        draw.rectangle([lx, ly, lx + 22 * _SS, ly + 14 * _SS], fill=color)
        draw.text((lx + 30 * _SS, ly + 7 * _SS), name, font=f_leg, fill=_TEXT, anchor="lm")
        ly += 24 * _SS

    img = img.resize((W // _SS, H // _SS), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
