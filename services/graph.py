"""render an event points-over-time line graph (tier cutoff and/or a followed player) to a png.

fed by /api/events/graph, whose GraphSeries has `tier` and `user` as [[ms, score], ...].
"""

from __future__ import annotations

import datetime
import io
import math

from PIL import Image, ImageDraw

from services.leaderboard import _font

_BG = (17, 17, 17, 255)
_GRID = (44, 48, 58, 255)
_TEXT = (242, 245, 250, 255)
_MUTED = (150, 162, 178, 255)
_PRED = (255, 190, 90, 255)  # dashed projection line
_SS = 2  # supersample

_HOUR_MS = 3_600_000
# robonene's "standard" heatmap palette (low gain -> high gain), evenly spaced. kept identical
# to the website (EventHeatmap.tsx) so both render the same colour weight
_HEAT_STOPS = [
    (0.000, (252, 212, 220)),
    (0.125, (236, 226, 240)),
    (0.250, (208, 209, 230)),
    (0.375, (166, 189, 219)),
    (0.500, (103, 169, 207)),
    (0.625, (54, 144, 192)),
    (0.750, (139, 116, 189)),
    (0.875, (121, 83, 169)),
    (1.000, (48, 25, 52)),
]
_HEAT_EMPTY = (22, 25, 33, 255)  # outside the event's data window


def _fmt_score(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


def _fmt_gain(n: float) -> str:
    # in-cell hourly gain, e.g. +2.2m / +145k / +2.2k / +0
    if n >= 1_000_000:
        return f"+{n / 1_000_000:.1f}m"
    if n >= 10_000:
        return f"+{n / 1_000:.0f}k"
    if n >= 1_000:
        return f"+{n / 1_000:.1f}k"
    return f"+{int(n)}"


def _fmt_time(ms: float, tz: datetime.tzinfo) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz).strftime("%m/%d %H:%M")


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    pts: list[tuple[float, float]],
    color: tuple[int, int, int, int],
    width: int,
    dash: int,
    gap: int,
) -> None:
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = math.hypot(bx - ax, by - ay)
        if seg == 0:
            continue
        pos = 0.0
        while pos < seg:
            s, e = pos / seg, min(1.0, (pos + dash) / seg)
            draw.line(
                [(ax + (bx - ax) * s, ay + (by - ay) * s), (ax + (bx - ax) * e, ay + (by - ay) * e)],
                fill=color,
                width=width,
            )
            pos += dash + gap


def render_graph(
    lines: list[tuple[str, list, tuple[int, int, int, int]]],
    title: str,
    *,
    tz: datetime.tzinfo | None = None,
    prediction: dict | None = None,
) -> bytes:
    tz = tz or datetime.timezone.utc
    points = [(int(x), float(y)) for _, series, _ in lines for x, y in series]
    if prediction:  # let the projection widen the axes so it fits on the plot
        points += [(int(x), float(y)) for x, y in prediction["points"]]
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

    legend: list[tuple[str, tuple[int, int, int, int]]] = [(n, c) for n, _, c in lines]
    if prediction and prediction.get("points"):
        proj = [(px(int(x)), py(float(y))) for x, y in prediction["points"]]
        _dashed_line(draw, proj, _PRED, 3 * _SS, 12 * _SS, 8 * _SS)
        fx, fy = proj[-1]
        draw.ellipse([fx - 5 * _SS, fy - 5 * _SS, fx + 5 * _SS, fy + 5 * _SS], fill=_PRED)
        draw.text(
            (fx - 8 * _SS, fy), _fmt_score(prediction["final"]), font=f_axis, fill=_PRED, anchor="rm"
        )
        legend.append((f"Predicted ~{_fmt_score(prediction['final'])}", _PRED))

    # legend (top-right)
    lx, ly = x1 - 200 * _SS, 26 * _SS
    for name, color in legend:
        draw.rectangle([lx, ly, lx + 22 * _SS, ly + 14 * _SS], fill=color)
        draw.text((lx + 30 * _SS, ly + 7 * _SS), name, font=f_leg, fill=_TEXT, anchor="lm")
        ly += 24 * _SS

    img = img.resize((W // _SS, H // _SS), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def _cum_at(pts: list[tuple[int, float]], ts: float) -> float:
    # cumulative score at ts by linear interpolation, clamped at the ends
    if ts <= pts[0][0]:
        return pts[0][1]
    if ts >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if a[0] <= ts < b[0]:
            f = (ts - a[0]) / (b[0] - a[0]) if b[0] != a[0] else 0.0
            return a[1] + f * (b[1] - a[1])
    return pts[-1][1]


def _lerp3(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _heat_color(t: float) -> tuple[int, int, int, int]:
    t = max(0.0, min(1.0, t))
    for i in range(len(_HEAT_STOPS) - 1):
        t0, c0 = _HEAT_STOPS[i]
        t1, c1 = _HEAT_STOPS[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return (*_lerp3(c0, c1, f), 255)
    return (*_HEAT_STOPS[-1][1], 255)


def render_heatmap(
    points: list,
    title: str,
    *,
    tz: datetime.tzinfo | None = None,
    start_time: int | None = None,
) -> bytes:
    """day x hour grid coloured by event points gained per hour (EPH), from a cumulative series."""
    tz = tz or datetime.timezone.utc
    pts = sorted((int(x), float(y)) for x, y in points)
    if len(pts) < 2:
        img = Image.new("RGBA", (600, 200), _BG)
        d = ImageDraw.Draw(img)
        d.text((20, 90), "Not enough data yet.", font=_font(24), fill=_MUTED)
        out = io.BytesIO()
        img.convert("RGB").save(out, "PNG")
        return out.getvalue()

    first, last = pts[0][0], pts[-1][0]
    start = int(start_time) if start_time else first
    start_dt = datetime.datetime.fromtimestamp(start / 1000, tz)
    start_date = start_dt.date()

    # bucket each clock hour's gain into (day, hour). the hour anchor floors to the local hour so
    # columns line up with wall-clock hours
    anchor = int(start_dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    grid: dict[tuple[int, int], float] = {}
    max_day = 1
    max_val = 0.0
    b = anchor
    while b < last:
        nb = b + _HOUR_MS
        if nb > first:  # skip hours entirely before the first sample
            gain = max(0.0, _cum_at(pts, min(nb, last)) - _cum_at(pts, max(b, first)))
            dt = datetime.datetime.fromtimestamp(b / 1000, tz)
            day = (dt.date() - start_date).days + 1
            if day >= 1:
                grid[(day, dt.hour)] = gain
                max_day = max(max_day, day)
                max_val = max(max_val, gain)
        b = nb
    max_val = max_val or 1.0

    label_w = 96 * _SS
    cell_w, cell_h = 46 * _SS, 30 * _SS
    head_h = 24 * _SS
    title_h = 46 * _SS
    legend_h = 44 * _SS
    grid_w = 24 * cell_w
    W = label_w + grid_w + 20 * _SS
    H = title_h + head_h + max_day * cell_h + legend_h
    gx0 = label_w
    gy0 = title_h + head_h

    img = Image.new("RGBA", (W, H), _BG)
    draw = ImageDraw.Draw(img)
    f_title = _font(24 * _SS)
    f_axis = _font(13 * _SS)
    f_cell = _font(11 * _SS)
    f_day = _font(13 * _SS)

    draw.text((14 * _SS, 14 * _SS), title, font=f_title, fill=_TEXT)

    # hour labels across the top
    for h in range(24):
        cx = gx0 + h * cell_w + cell_w // 2
        draw.text((cx, gy0 - 6 * _SS), str(h), font=f_axis, fill=_MUTED, anchor="mb")

    for day in range(1, max_day + 1):
        cy = gy0 + (day - 1) * cell_h
        day_dt = start_date + datetime.timedelta(days=day - 1)
        draw.text(
            (label_w - 10 * _SS, cy + cell_h // 2),
            f"{day_dt.strftime('%a')} Day {day}",
            font=f_day,
            fill=_MUTED,
            anchor="rm",
        )
        for h in range(24):
            cx = gx0 + h * cell_w
            val = grid.get((day, h))
            if val is None:
                color = _HEAT_EMPTY
            elif val <= 0:
                color = (255, 255, 255, 255)  # zero gain: white, off the gradient
            else:
                color = _heat_color(val / max_val)
            draw.rectangle([cx, cy, cx + cell_w - _SS, cy + cell_h - _SS], fill=color)
            if val is not None:
                bright = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
                draw.text(
                    (cx + cell_w // 2, cy + cell_h // 2),
                    _fmt_gain(val),
                    font=f_cell,
                    fill=_TEXT if bright < 150 else _BG,
                    anchor="mm",
                )

    # legend: a white "+0" swatch (off the gradient) then the gradient bar
    ly = gy0 + max_day * cell_h + 14 * _SS
    sw = 16 * _SS
    draw.rectangle([gx0, ly, gx0 + sw, ly + 12 * _SS], fill=(255, 255, 255, 255))
    draw.text((gx0 + sw // 2, ly + 16 * _SS), "+0", font=f_axis, fill=_MUTED, anchor="mt")
    bar_x = gx0 + sw + 22 * _SS
    lw = 200 * _SS
    for i in range(lw):
        draw.line(
            [(bar_x + i, ly), (bar_x + i, ly + 12 * _SS)], fill=_heat_color(i / lw), width=1
        )
    draw.text((bar_x + lw, ly + 16 * _SS), _fmt_score(max_val), font=f_axis, fill=_MUTED, anchor="rt")
    draw.text(
        (bar_x + lw + 16 * _SS, ly + 6 * _SS),
        "event points / hour",
        font=f_axis,
        fill=_MUTED,
        anchor="lm",
    )

    img = img.resize((W // _SS, H // _SS), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
