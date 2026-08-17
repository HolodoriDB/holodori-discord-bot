"""Shared holomem chapter-switch buttons for the event commands (leaderboard, cutoff, heatmap).

One row (or rows) of buttons, one per chapter: green+disabled = the chapter shown now, blue = a
finished/ongoing chapter to jump to, grey+disabled = a chapter that has not started yet. 5 per row.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

if TYPE_CHECKING:
    from data.models import EventInfo

# (chapter_id, short label, started). "started" = finished or ongoing (not a future chapter).
Chapter = tuple[str, str, bool]
_MAX_ROWS = 4  # leave a row for the command's own controls (song dropdown / page buttons)


def chapters_from_event(ev: "EventInfo | None") -> list[Chapter]:
    # build the switchable chapter list from the event's per-chapter metadata; empty for a
    # single-chapter event (no buttons needed)
    if not ev or len(ev.chapterMeta) <= 1:
        return []
    now_ms = int(time.time() * 1000)
    out: list[Chapter] = []
    for i, (cid, cm) in enumerate(ev.chapterMeta.items()):
        started = cm.startTime is None or cm.startTime <= now_ms
        label = cm.shortName or cm.name or f"Ch {i + 1}"
        out.append((cid, label, started))
    return out


class ChapterButtons:
    """Composed into a HoloView. `add_to(view)` places the buttons; a switchable chapter click calls
    `on_select(interaction, chapter_id)`. The owner re-renders and calls `set_current(cid)`."""

    def __init__(
        self,
        chapters: list[Chapter],
        current_chapter: str | None,
        on_select: Callable[[discord.Interaction, str], Awaitable[None]],
        *,
        start_row: int = 0,
        max_rows: int = _MAX_ROWS,
    ) -> None:
        self.chapters = chapters or []
        self.current = current_chapter
        self._on_select = on_select
        self._start_row = start_row
        self._max_rows = max_rows
        self.items: list[tuple[discord.ui.Button, str, bool]] = []

    @property
    def active(self) -> bool:
        return len(self.chapters) > 1

    @property
    def rows_used(self) -> int:
        n = min(len(self.chapters), self._max_rows * 5)
        return math.ceil(n / 5) if n else 0

    def add_to(self, view: discord.ui.View) -> None:
        if not self.active:
            return
        for i, (cid, label, started) in enumerate(self.chapters[: self._max_rows * 5]):
            btn = discord.ui.Button(
                label=(label or "?")[:80], row=self._start_row + i // 5
            )
            self._style(btn, cid, started)

            async def cb(interaction: discord.Interaction, cid: str = cid) -> None:
                if cid == self.current:
                    await interaction.response.defer()
                    return
                await self._on_select(interaction, cid)

            btn.callback = cb  # type: ignore[method-assign]
            view.add_item(btn)
            self.items.append((btn, cid, started))

    def set_current(self, cid: str) -> None:
        self.current = cid
        for btn, c, started in self.items:
            self._style(btn, c, started)

    def label_of(self, cid: str | None) -> str | None:
        return next((label for c, label, _ in self.chapters if c == cid), None)

    def _style(self, btn: discord.ui.Button, cid: str, started: bool) -> None:
        if cid == self.current:
            btn.style = discord.ButtonStyle.success  # green: you're on this page
            btn.disabled = True
        elif not started:
            btn.style = discord.ButtonStyle.secondary  # grey: not started yet
            btn.disabled = True
        else:
            btn.style = discord.ButtonStyle.primary  # blue: jump to it
            btn.disabled = False
