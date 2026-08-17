"""RoboNene-style text leaderboard, rendered as one monospace code block.

Columns T (rank + tier move), Name, Score, Change; the name column is squeezed to keep a row within a
target width, and every column pads by DISPLAY width (CJK/Hangul count as 2 cells) so a wide name
doesn't shove later columns. Ported from RoboNene's generateRankingTextChanges
(github.com/Ai0796/RoboNene). NB: a fenced block can render CJK as tofu on some clients (the old
per-row inline `code` spans avoided that); the display-width padding keeps columns aligned regardless.
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
    if change > 0:
        return f"(↑{change})"
    if change < 0:
        return f"(↓{-change})"
    return "(-)"


def _change_str(v: int | None) -> str:
    # signed for a real gain/loss; "-" for none or unknown (matches the old board)
    return f"{v:+,}" if v else "-"


def _clean(name: str) -> str:
    return _CLEAN.sub("", name).replace("`", "'").strip() or "?"


def format_leaderboard(rows: list[LBRow], *, mobile: bool = False) -> str:
    if not rows:
        return "​"  # zero-width space: an embed field/description can't be empty
    max_len = MOBILE_MAX if mobile else DESKTOP_MAX
    names = [_clean(r.name) for r in rows]
    rank_lbl, name_lbl, score_lbl, change_lbl = "T", "Name", "Score", "Change"

    rank_w = max(len(rank_lbl), *(len(str(r.rank)) for r in rows))
    tag_w = max(len(_tier_tag(r.rank_change)) for r in rows)
    name_w = max(len(name_lbl), *(_width(n) for n in names))  # display cells, not char count
    score_w = max(len(score_lbl), *(len(f"{r.score:,}") for r in rows))
    change_w = max(len(change_lbl), *(len(_change_str(r.score_change)) for r in rows))

    # squeeze only the name column so the row fits the target width
    over = max(0, (rank_w + tag_w + name_w + score_w + change_w) - max_len)
    name_w = max(len(name_lbl), name_w - over)

    def row(rank: str, name: str, score: str, change: str, star: bool = False) -> str:
        return f"{rank} {name} {score} {change}" + (" *" if star else "")

    out = [
        row(
            # "T" right-aligned over the rank digits, blank over the tier-tag column (was floated to
            # the far right of rank+tag, which read as misaligned)
            rank_lbl.rjust(rank_w) + " " * tag_w,
            _fit(name_lbl, name_w),
            score_lbl.ljust(score_w),
            change_lbl.rjust(change_w),
        )
    ]
    for r, name in zip(rows, names):
        rank = str(r.rank).rjust(rank_w) + _tier_tag(r.rank_change).ljust(tag_w)
        out.append(
            row(
                rank,
                _fit(name, name_w),  # truncate + pad by display width so CJK names stay aligned
                f"{r.score:,}".rjust(score_w),
                _change_str(r.score_change).rjust(change_w),
                star=r.is_you,
            )
        )
    return "```\n" + "\n".join(out) + "\n```"
