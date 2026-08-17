"""RoboNene-style text leaderboard, rendered as one monospace code block.

Columns T (rank + tier move), Name, Score, Change; the name column is squeezed to a target width and
every column pads by DISPLAY width (CJK = 2 cells). Ported from RoboNene's generateRankingTextChanges
(github.com/Ai0796/RoboNene). Known limitation: Discord renders CJK/Hangul at a non-2x width (and it
differs desktop vs mobile), so a CJK name shifts whatever column follows it - true in inline `code`
too, so it's NOT an inline-vs-block thing. The only reliable text fix is to make the name the LAST
column so nothing follows it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

DESKTOP_MAX = 42  # target row width (cells); the name column shrinks to fit it
MOBILE_MAX = 30

_CLEAN = re.compile(r"[\n\t\r]")


def _cw(ch: str) -> int:
    # display cells: CJK/Hangul/fullwidth render 2-wide in Discord's monospace, everything else 1.
    # padding by len() instead of this is what let a name like "하젤" shove the later columns right.
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _width(s: str) -> int:
    return sum(_cw(c) for c in s)


def _fit(s: str, w: int) -> str:
    # left-align s in exactly w display cells: cut on a cell boundary, then pad with spaces
    used = 0
    out: list[str] = []
    for ch in s:
        c = _cw(ch)
        if used + c > w:
            break
        out.append(ch)
        used += c
    return "".join(out) + " " * (w - used)


@dataclass
class LBRow:
    rank: int
    name: str
    score: int
    rank_change: int = 0  # tier movement since the last snapshot: + up, - down, 0 flat
    score_change: int | None = None  # points gained since the last snapshot; None = unknown
    is_you: bool = False


def _tier_tag(change: int) -> str:
    # compact rank-move suffix stuck to the rank ("6↑1", "16↓4", "1-") - no parens, to keep the row
    # narrow enough to fit the embed on desktop
    if change > 0:
        return f"↑{change}"
    if change < 0:
        return f"↓{-change}"
    return "-"


def _change_str(v: int | None) -> str:
    # the gain; 0 shows as "0", unknown as "-". no + sign (saves a column cell)
    return "-" if v is None else f"{v:,}"


def _clean(name: str) -> str:
    return _CLEAN.sub("", name).replace("`", "'").strip() or "?"


def format_leaderboard(rows: list[LBRow], *, mobile: bool = False) -> str:
    if not rows:
        return "​"  # zero-width space: an embed field/description can't be empty
    max_len = MOBILE_MAX if mobile else DESKTOP_MAX
    names = [_clean(r.name) for r in rows]
    rank_lbl, name_lbl, score_lbl, change_lbl = "#", "Name", "Score", "+/hr"

    # the "#" column is the rank with its move-tag stuck on ("6↑1"), left-aligned as one cell
    ranks = [str(r.rank) + _tier_tag(r.rank_change) for r in rows]
    rank_w = max(len(rank_lbl), *(_width(s) for s in ranks))
    name_w = max(len(name_lbl), *(_width(n) for n in names))  # display cells, not char count
    score_w = max(len(score_lbl), *(len(f"{r.score:,}") for r in rows))
    change_w = max(len(change_lbl), *(len(_change_str(r.score_change)) for r in rows))

    # squeeze only the name column so the WHOLE row (incl. the 3 single-space separators) fits the
    # target width - not counting the separators is what let the desktop board overrun the embed
    over = max(0, (rank_w + name_w + score_w + change_w + 3) - max_len)
    name_w = max(len(name_lbl), name_w - over)

    def row(rank: str, name: str, score: str, change: str, star: bool = False) -> str:
        return f"{rank} {name} {score} {change}" + (" *" if star else "")

    out = [
        row(
            _fit(rank_lbl, rank_w),
            _fit(name_lbl, name_w),
            score_lbl.rjust(score_w),
            change_lbl.rjust(change_w),
        )
    ]
    for r, name, rk in zip(rows, names, ranks):
        out.append(
            row(
                _fit(rk, rank_w),  # rank+tag, left-aligned
                _fit(name, name_w),  # truncate + pad by display width so CJK names stay aligned
                f"{r.score:,}".rjust(score_w),
                _change_str(r.score_change).rjust(change_w),
                star=r.is_you,
            )
        )
    return "```\n" + "\n".join(out) + "\n```"
