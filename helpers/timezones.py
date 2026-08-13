"""timezone resolution shared by /user timezone and /graph: common aliases or IANA names."""

from __future__ import annotations

from datetime import tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALIASES = {
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "gmt": "UTC",
    "utc": "UTC",
    "jst": "Asia/Tokyo",
    "kst": "Asia/Seoul",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
}

COMMON = "ET, CT, MT, PT, JST, KST, GMT"


def canonical(name: str) -> str | None:
    """the IANA zone name for a common alias or valid IANA input, or None if invalid."""
    name = (name or "").strip()
    for cand in (ALIASES.get(name.lower()), name):
        if not cand:
            continue
        try:
            ZoneInfo(cand)
            return cand
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return None


def resolve(name: str) -> tzinfo:
    return ZoneInfo(canonical(name) or "UTC")
