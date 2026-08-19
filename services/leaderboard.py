"""RoboNene-style text leaderboard, rendered as one ```ansi code block.

Columns # (rank + coloured move tag: green up / red down), Name, Score, +/hr; the name column is
squeezed so the WHOLE row (incl. separators) fits the target width, and every column pads by DISPLAY
width, computed per GRAPHEME via wcwidth. A multi-codepoint emoji (a ZWJ sequence, a flag, a keycap, a
skin-toned face) counts as ONE 2-cell glyph instead of several (counting those by code point
overcounts them badly - a family emoji was 8 - and shoved the score/+ columns right). ansi escapes are
zero-width so they must NOT count toward padding (see _rank_cell). Ported from RoboNene's
generateRankingTextChanges (github.com/Ai0796/RoboNene).

CJK width: Discord's code font draws a CJK/Hangul/Kana char at ~1.66 EN cells, NOT 2, so counting it
as 2 left CJK names too narrow (the more CJK, the shorter). We count the true fractional width
(_CJK_WIDTH) and pad the fractional leftover with THREE-PER-EM spaces (~1/3 cell) - see _pad. This is
a heuristic and TUNABLE: the real ratio differs desktop vs mobile, and it only helps if the client
draws U+2004 fractionally rather than snapping every space to a full cell. If it can't be dialled in,
the fully reliable fallback is to make Name the LAST column so nothing follows it."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from wcwidth import wcswidth

DESKTOP_MAX = 42  # target row width (cells); the name column shrinks to fit it
MOBILE_MAX = 30

_CLEAN = re.compile(r"[\n\t\r]")


_RI_LO, _RI_HI = 0x1F1E6, 0x1F1FF  # regional indicators (flag emoji come in pairs)
_SKIN_LO, _SKIN_HI = 0x1F3FB, 0x1F3FF  # emoji skin-tone modifiers


def _is_regional(ch: str) -> bool:
    return _RI_LO <= ord(ch) <= _RI_HI


def _is_extend(ch: str) -> bool:
    # a char that shapes ONTO the previous one and adds no width of its own: a combining mark, a
    # variation selector, the keycap combiner, or an emoji skin-tone modifier
    o = ord(ch)
    return (
        unicodedata.category(ch) in ("Mn", "Mc", "Me")
        or 0xFE00 <= o <= 0xFE0F
        or 0xE0100 <= o <= 0xE01EF
        or o == 0x20E3
        or _SKIN_LO <= o <= _SKIN_HI
    )


def _graphemes(s: str) -> list[str]:
    # split into grapheme clusters (approx, tuned for the emoji cases that break alignment): a base
    # plus its combining marks / variation selectors, ZWJ-joined emoji, and flag (regional-indicator)
    # pairs - so a multi-codepoint emoji counts as ONE glyph, not several.
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        i += 1
        if _is_regional(ch):  # a flag is two regional indicators
            if i < n and _is_regional(s[i]):
                ch += s[i]
                i += 1
            out.append(ch)
            continue
        while i < n:
            nxt = s[i]
            if nxt == "\u200d":  # ZWJ: absorb it and the emoji it joins to
                ch += nxt + (s[i + 1] if i + 1 < n else "")
                i += 2
                continue
            if _is_extend(nxt):
                ch += nxt
                i += 1
                continue
            break
        out.append(ch)
    return out


# A CJK/Kana/Hangul char renders ~1.66 EN cells in Discord's code font (NOT 2), so counting it as 2 left
# CJK names too narrow (the more CJK, the shorter). We count the true fractional width and pad the
# leftover with a fractional-width space. EMPIRICALLY TUNED against screenshots: 1.66 lands the 3/6/9-
# char cases dead-on (they pad with ZERO fractional glyphs, so they're the ground truth); 5/3 measured a
# hair too wide and pushed them slightly under. Nudge _CJK_WIDTH if the multiples of 3 ever drift.
_CJK_WIDTH = 1.66
# the leftover at other counts is 1/3 or 2/3 of a cell, padded with this glyph, which must render at
# EXACTLY 1/3 of a Latin cell. U+2004 (1/3 EM) was a touch too WIDE; U+2005 (1/4 EM) is very close.
# TUNING LADDER, widest -> narrowest, swap this one line if the +1/3 and +2/3 cases over/undershoot:
#   U+2004 (1/3 em) . U+2005 (1/4 em) . U+2009 (thin) . U+2006 (1/6 em) . U+200A (hair)
_THIRD = "\u2005"  # FOUR-PER-EM SPACE
_THIRD_W = 1 / 3  # we always quantise the pad to 1/3-cell units; _THIRD must BE that 1/3 cell
# NOTE: even a perfect tune here holds for ONE client - Discord's CJK/space widths differ desktop vs
# mobile, so if the two can't be reconciled the only client-independent fix is Name as the LAST column.


def _is_cjk_letter(cluster: str) -> bool:
    # a plain CJK / Kana / Hangul character (renders ~1.66x), NOT a wide emoji glyph (renders ~2x):
    # every code point is a wide/fullwidth LETTER, so emoji symbols (category S*) don't qualify
    return all(
        unicodedata.east_asian_width(c) in ("W", "F")
        and unicodedata.category(c).startswith("L")
        for c in cluster
    )


def _cw(cluster: str) -> float:
    # display cells for one grapheme (EN-cell units). wcswidth knows CJK/fullwidth (2), zero-width
    # combining marks + variation selectors (0), and flags / keycaps / ZWJ emoji sequences (2, as one
    # glyph). a plain CJK letter is then scaled to its true ~1.66 (emoji stay 2). wcswidth returns -1
    # on a control char, so fall back to an east-asian estimate (controls as 0).
    w: float = wcswidth(cluster)
    if w < 0:
        w = sum(
            0
            if unicodedata.category(c)[0] == "C"
            else 2
            if unicodedata.east_asian_width(c) in ("W", "F")
            else 1
            for c in cluster
        )
    if w >= 2 and _is_cjk_letter(cluster):
        return _CJK_WIDTH
    return float(w)


def _width(s: str) -> float:
    return sum(_cw(g) for g in _graphemes(s))


def _pad(width: float) -> str:
    # spaces filling `width` EN cells: whole EN spaces for the integer part + THREE-PER-EM spaces for
    # the ~1/3 remainder, so a fractional CJK deficit is compensated. rounds to the nearest 1/3 cell.
    thirds = max(0, int(round(width / _THIRD_W)))
    return " " * (thirds // 3) + _THIRD * (thirds % 3)


def _fit(s: str, w: float) -> str:
    # left-align s in ~w display cells: take whole graphemes until the next would overflow (an emoji is
    # never split mid-glyph), then pad the remainder with EN + three-per-em spaces
    used = 0.0
    out: list[str] = []
    for g in _graphemes(s):
        c = _cw(g)
        if used + c > w + 1e-6:
            break
        out.append(g)
        used += c
    return "".join(out) + _pad(w - used)


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


_ESC = ""  # for the ```ansi block: green ↑ / red ↓ move tags


def _rank_cell(rank: int, change: int, width: float) -> str:
    # "6↑1" with the move tag coloured (green up / red down), left-aligned and padded to `width` by
    # the tag's VISIBLE length - the ansi escapes are zero-width so must NOT count toward padding
    tag = _tier_tag(change)
    if change > 0:
        colored = f"{_ESC}[32m{tag}{_ESC}[0m"
    elif change < 0:
        colored = f"{_ESC}[31m{tag}{_ESC}[0m"
    else:
        colored = tag
    return f"{rank}{colored}{_pad(max(0.0, width - _width(f'{rank}{tag}')))}"


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
    for r, name in zip(rows, names):
        out.append(
            row(
                _rank_cell(r.rank, r.rank_change, rank_w),  # rank + coloured move tag, left-aligned
                _fit(name, name_w),  # truncate + pad by display width so CJK names stay aligned
                f"{r.score:,}".rjust(score_w),
                _change_str(r.score_change).rjust(change_w),
                star=r.is_you,
            )
        )
    return "```ansi\n" + "\n".join(out) + "\n```"
