"""Shared parsing for the %-prefix text commands (cutoff / graph / heatmap / leaderboard).

Region and tier can be given in any order and with no arg names, e.g. `%cutoff us t50`,
`%cutoff 25 asia`, `%graph t100`. Regions are fuzzy-matched (a transposition or one-off typo
still resolves) so `asian`, `aisa`, `pj`, `u` all work.
"""

from __future__ import annotations

import re

import discord

from helpers import embeds

# every region's canonical code plus the spellings we accept for it
_REGION_ALIASES: dict[str, list[str]] = {
    "us": ["us", "usa", "global", "glob", "america", "na", "world", "en", "eng"],
    "as": ["as", "asia", "asian"],
    "jp": ["jp", "japan", "japanese", "jpn", "nihon"],
}

# a tier token: a bare rank or a T-prefixed one (1, t100, 500, T1000)
_TIER_RE = re.compile(r"^[tT]?(\d{1,7})$")


def _osa_distance(a: str, b: str) -> int:
    # optimal string alignment distance: levenshtein plus adjacent transpositions, so "pj"->"jp"
    # and "aisa"->"asia" count as one edit. tiny strings, so the full matrix is fine.
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def match_region(token: str) -> str | None:
    """resolve one token to a region code (us/as/jp), or None if it isn't clearly a region."""
    t = token.lower()
    if not t:
        return None
    for region, aliases in _REGION_ALIASES.items():
        if t in aliases:
            return region
    # a prefix of exactly one region's aliases ("u" -> us, "j" -> jp; "a" is ambiguous, so None)
    prefixed = {r for r, al in _REGION_ALIASES.items() if any(a.startswith(t) for a in al)}
    if len(prefixed) == 1:
        return next(iter(prefixed))
    # otherwise the closest alias within one edit, if that points at a single region
    best_d = 99
    best: set[str] = set()
    for region, aliases in _REGION_ALIASES.items():
        d = min(_osa_distance(t, a) for a in aliases)
        if d < best_d:
            best_d, best = d, {region}
        elif d == best_d:
            best.add(region)
    if best_d <= 1 and len(best) == 1:
        return next(iter(best))
    return None


def parse_region_tier(tokens: list[str]) -> tuple[str | None, int | None, list[str]]:
    """(region_code_or_None, tier_or_None, leftover_unmatched). first number wins the tier, first
    region-looking token wins the region; anything else is leftover (a typo / junk)."""
    region: str | None = None
    tier: int | None = None
    leftover: list[str] = []
    for tok in tokens:
        m = _TIER_RE.match(tok)
        if m and tier is None:
            tier = int(m.group(1))
            continue
        reg = match_region(tok)
        if reg and region is None:
            region = reg
            continue
        leftover.append(tok)
    return region, tier, leftover


def help_embed(name: str, params: str, *, any_order: bool, aliases: list[str]) -> discord.Embed:
    """the fallback usage card shown when a %-command is called with no valid parameter combo.

    params spells the args like `[region] {tier}` (square = optional, curly = required)."""
    usage = f"Usage: `%{name} {params}`".rstrip()
    if any_order:
        usage += " (accepts any input order)"
    al = ", ".join(f"%{a}" for a in aliases) if aliases else "None"
    return embeds.embed(title=f"Command %{name}", description=f"{usage}\nAliases: `{al}`")
