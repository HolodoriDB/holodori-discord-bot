"""render an event points-over-time line graph (tier cutoff and/or a followed player) to a png.

fed by /api/events/graph, whose GraphSeries has `tier` and `user` as [[ms, score], ...].
"""

from __future__ import annotations

import bisect
import datetime
import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

# a bundled ttf, then common system fonts, then pillow's sized default, so it renders on windows and
# the linux server (the leaderboard used to share this before it became a text table)
_FONT_CANDIDATES = [
    "data/assets/fonts/font.ttf",
    "data/assets/fonts/font-bold.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
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
# data-quality markers (copied from sbuga-bot): MD/PD are our fetch gaps, ND/N+ are the player
# being off the top 100
_X_FILL = (36, 27, 31, 255)  # hour outside the event window (drawn with a red X)
_X_LINE = (214, 68, 78, 255)
_MD_FILL = (48, 24, 27, 255)  # "MD": our fetches failed for most of the hour
_MD_TEXT = (214, 68, 78, 255)
_ND_FILL = (28, 31, 40, 255)  # "ND": fetched all hour but the player wasn't on the top 100
_ND_TEXT = (150, 162, 200, 255)
_FLAG_PD = (255, 205, 70, 255)  # yellow "*": partial data (a real fetch gap)
_FLAG_NP = (96, 176, 240, 255)  # blue "+": N+ (player off the top 100 part of the hour)
_COVER_MS = 60_000  # each fetch covers +-this; larger gaps count as missing time
_PD_MISSING_MS = 120_000  # >2 min gap in an hour -> partial data


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


def _cum_at(pts: list[tuple[int, float]], xs: list[int], ts: float) -> float:
    # cumulative score at ts by linear interpolation, clamped; xs = [p[0] ...] for a bisect seek
    if ts <= pts[0][0]:
        return pts[0][1]
    if ts >= pts[-1][0]:
        return pts[-1][1]
    i = bisect.bisect_right(xs, ts)
    a, b = pts[i - 1], pts[i]
    f = (ts - a[0]) / (b[0] - a[0]) if b[0] != a[0] else 0.0
    return a[1] + f * (b[1] - a[1])


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


def _covered_intervals(times: list[int]) -> list[tuple[int, int]]:
    # merge each fetch's +-_COVER_MS window into disjoint covered intervals (sorted input)
    merged: list[tuple[int, int]] = []
    for t in times:
        s, e = t - _COVER_MS, t + _COVER_MS
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _missing_ms(merged: list[tuple[int, int]], lo: int, hi: int) -> int:
    # ms of [lo, hi) not inside any covered interval (a real gap between fetches)
    covered = 0
    for s, e in merged:
        a, b = max(s, lo), min(e, hi)
        if b > a:
            covered += b - a
    return max(0, (hi - lo) - covered)


def _small_img(text: str) -> bytes:
    img = Image.new("RGBA", (600, 200), _BG)
    ImageDraw.Draw(img).text((20, 90), text, font=_font(24), fill=_MUTED)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def _wrap_px(draw, text: str, font, max_w: float) -> list[str]:
    # greedy word-wrap so a long legend line fits the image width instead of being cut off
    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def render_heatmap(
    value_series: list,
    title: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    now_ms: int,
    coverage: list[int] | None = None,
    presence: list[int] | None = None,
    tz: datetime.tzinfo | None = None,
) -> bytes:
    """day x hour grid coloured by event points gained per hour (EPH).

    `value_series` is the cumulative [[ms, score], ...] to derive EPH from. `coverage` is the fetch
    timestamps (defaults to the series' own) used to flag our gaps as MD/PD. `presence` (player
    mode) is the timestamps the player was on the top 100, used to flag ND (off all hour) / N+.
    """
    tz = tz or datetime.timezone.utc
    pts = sorted((int(x), float(y)) for x, y in value_series)
    if len(pts) < 2 or not start_ms or not end_ms or end_ms <= start_ms:
        return _small_img("Not enough data yet.")
    xs = [p[0] for p in pts]
    cov = sorted(int(t) for t in (coverage if coverage is not None else xs))
    merged = _covered_intervals(cov)
    pres = sorted(int(t) for t in presence) if presence is not None else None
    is_user = pres is not None

    start_dt = datetime.datetime.fromtimestamp(start_ms / 1000, tz)
    end_dt = datetime.datetime.fromtimestamp(end_ms / 1000, tz)
    day_one = start_dt.date()
    num_days = max(1, (end_dt.date() - day_one).days + 1)

    # classify every cell: outside / future / md / nd / (count, val, plus, pd)
    cells: dict[tuple[int, int], tuple] = {}
    max_val = 1.0
    has_md = has_pd = has_nd = has_np = False
    for row in range(num_days):
        for hour in range(24):
            cs = int(
                datetime.datetime.combine(
                    day_one + datetime.timedelta(days=row), datetime.time(hour=hour), tzinfo=tz
                ).timestamp()
                * 1000
            )
            ce = cs + _HOUR_MS
            if ce <= start_ms or cs >= end_ms:
                cells[(row, hour)] = ("outside",)
                continue
            if cs > now_ms:
                cells[(row, hour)] = ("future",)
                continue
            lo, hi = max(cs, start_ms), min(ce, end_ms, now_ms)
            window = max(1, hi - lo)
            missing = _missing_ms(merged, lo, hi)
            if missing > window / 2:
                cells[(row, hour)] = ("md",)
                has_md = True
                continue
            if is_user and pres is not None:
                p_ct = bisect.bisect_right(pres, hi) - bisect.bisect_left(pres, lo)
                if p_ct == 0:  # fetched all hour, never on the top 100
                    cells[(row, hour)] = ("nd",)
                    has_nd = True
                    continue
                c_ct = bisect.bisect_right(cov, hi) - bisect.bisect_left(cov, lo)
                plus = (c_ct - p_ct) > 0  # off the top 100 part of the hour
            else:
                plus = False
            val = max(0.0, _cum_at(pts, xs, hi) - _cum_at(pts, xs, lo))
            pd = missing > _PD_MISSING_MS
            has_pd, has_np = has_pd or pd, has_np or plus
            cells[(row, hour)] = ("count", val, plus, pd)
            max_val = max(max_val, val)

    label_w = 110 * _SS
    cell_w, cell_h = 46 * _SS, 38 * _SS
    head_h = 24 * _SS
    title_h = 46 * _SS
    note_lines = 1 + has_md + has_pd + has_np + has_nd
    legend_h = 38 * _SS + note_lines * 20 * _SS  # extra top pad so the notes clear the gradient
    W = label_w + 24 * cell_w + 20 * _SS
    gy0 = title_h + head_h
    H = gy0 + num_days * cell_h + legend_h
    gx0 = label_w

    img = Image.new("RGBA", (W, H), _BG)
    draw = ImageDraw.Draw(img)
    f_title = _font(24 * _SS)
    f_axis = _font(13 * _SS)
    f_cell = _font(11 * _SS)
    f_flag = _font(15 * _SS)
    f_day = _font(13 * _SS)

    draw.text((14 * _SS, 14 * _SS), title, font=f_title, fill=_TEXT)
    for h in range(24):
        draw.text(
            (gx0 + h * cell_w + cell_w // 2, gy0 - 6 * _SS), str(h), font=f_axis, fill=_MUTED, anchor="mb"
        )

    for row in range(num_days):
        cy = gy0 + row * cell_h
        day_dt = day_one + datetime.timedelta(days=row)
        draw.text(
            (label_w - 10 * _SS, cy + cell_h // 2),
            f"{day_dt.strftime('%a')} Day {row + 1}",
            font=f_day,
            fill=_MUTED,
            anchor="rm",
        )
        for hour in range(24):
            cx = gx0 + hour * cell_w
            box = [cx, cy, cx + cell_w - _SS, cy + cell_h - _SS]
            kind = cells[(row, hour)]
            if kind[0] == "future":
                continue
            if kind[0] == "outside":
                draw.rectangle(box, fill=_X_FILL)
                m = 6 * _SS
                draw.line([(cx + m, cy + m), (cx + cell_w - m, cy + cell_h - m)], fill=_X_LINE, width=_SS)
                draw.line([(cx + m, cy + cell_h - m), (cx + cell_w - m, cy + m)], fill=_X_LINE, width=_SS)
                continue
            if kind[0] == "md":
                draw.rectangle(box, fill=_MD_FILL)
                draw.text((cx + cell_w // 2, cy + cell_h // 2), "MD", font=f_cell, fill=_MD_TEXT, anchor="mm")
                continue
            if kind[0] == "nd":
                draw.rectangle(box, fill=_ND_FILL)
                draw.text((cx + cell_w // 2, cy + cell_h // 2), "ND", font=f_cell, fill=_ND_TEXT, anchor="mm")
                continue
            _, val, plus, pd = kind
            color = (255, 255, 255, 255) if val <= 0 else _heat_color(val / max_val)
            draw.rectangle(box, fill=color)
            bright = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
            draw.text(
                (cx + cell_w // 2, cy + cell_h // 2),
                _fmt_gain(val),
                font=f_cell,
                fill=_TEXT if bright < 150 else _BG,
                anchor="mm",
            )
            if pd:  # partial data - yellow asterisk, top-right
                draw.text((cx + cell_w - 3 * _SS, cy + _SS), "*", font=f_flag, fill=_FLAG_PD, anchor="ra")
            if plus:  # off the top 100 part of the hour - blue plus, top-left
                draw.text((cx + 3 * _SS, cy + _SS), "+", font=f_flag, fill=_FLAG_NP, anchor="la")

    # legend: a white "+0" swatch (off the gradient) then the gradient bar
    ly = gy0 + num_days * cell_h + 14 * _SS
    sw = 16 * _SS
    draw.rectangle([gx0, ly, gx0 + sw, ly + 12 * _SS], fill=(255, 255, 255, 255))
    draw.text((gx0 + sw // 2, ly + 16 * _SS), "+0", font=f_axis, fill=_MUTED, anchor="mt")
    bar_x = gx0 + sw + 22 * _SS
    lw = 200 * _SS
    for i in range(lw):
        draw.line([(bar_x + i, ly), (bar_x + i, ly + 12 * _SS)], fill=_heat_color(i / lw), width=1)
    draw.text((bar_x + lw, ly + 16 * _SS), _fmt_score(max_val), font=f_axis, fill=_MUTED, anchor="rt")
    draw.text((bar_x + lw + 16 * _SS, ly + 6 * _SS), "event points / hour", font=f_axis, fill=_MUTED, anchor="lm")

    # marker legend: one line per marker that actually appears. left-aligned to the IMAGE edge
    # (like the title), not the grid, with a gap below the gradient bar
    ny = ly + 40 * _SS
    notes_x = 14 * _SS
    for on, text, col in (
        (has_md, "MD - Missing data. We failed to fetch data for most of this hour.", _MD_TEXT),
        (has_pd, "* - Partial data. We had some data gaps, so this hour's value may be off.", _FLAG_PD),
        (
            has_np,
            "+ - At least this much, could be more as they fell off the top 100 leaderboard and were untrackable for part of the hour.",
            _FLAG_NP,
        ),
        (has_nd, "ND - No data. They were not on the top 100 this hour.", _ND_TEXT),
    ):
        if on:
            for line in _wrap_px(draw, text, f_axis, W - notes_x - 14 * _SS):
                draw.text((notes_x, ny), line, font=f_axis, fill=col, anchor="lt")
                ny += 20 * _SS

    img = img.resize((W // _SS, H // _SS), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()
