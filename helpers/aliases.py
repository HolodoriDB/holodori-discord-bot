"""Shared formatting for the public `/{song,event,holomem} aliases` commands.

The full search-key set for one item can be dozens of entries (every language's name + every
romanization + split parts), so a one-per-line markdown list overflows the embed. Match sbuga-bot:
render each group (manual / automatic) as a single comma-joined, code-formatted field, trimmed to fit.
"""

from __future__ import annotations

_FIELD_LIMIT = 1024


def alias_field(values: list[str]) -> str:
    """Comma-joined aliases in one code span, trimmed to fit an embed field."""
    if not values:
        return "*None*"
    text = ", ".join(values)
    if len(text) + 2 > _FIELD_LIMIT:
        text = text[: _FIELD_LIMIT - 6].rsplit(", ", 1)[0] + ", …"
    return f"`{text}`"
