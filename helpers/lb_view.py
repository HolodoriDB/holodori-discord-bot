from __future__ import annotations

import math
from typing import Awaitable, Callable

import discord

from helpers import embeds
from helpers.chapter_buttons import Chapter, fanmark_emoji
from helpers.views import HoloView
from services.leaderboard import LBRow, format_leaderboard

PER_PAGE = 20
_MAX_CHAPTER_ROWS = 4  # leave one row (of the 5) for the page controls

Fetch = Callable[[str], Awaitable[list[dict]]]


def _to_row(r: dict) -> LBRow:
    ep = r.get("epChange")
    return LBRow(
        rank=int(r.get("rank") or 0),
        name=str(r.get("name") or "?"),
        score=int(r.get("score") or 0),
        rank_change=int(r.get("rankChange") or 0),
        score_change=int(ep) if ep is not None else None,
    )


class LeaderboardView(HoloView):
    """RoboNene-style text leaderboard: prev/next paging, an OFFSET half-page shift, and a MOBILE
    toggle that narrows the table. no image, so Discord renders every glyph (CN/JP names included).

    for a multi-chapter (relay) event it also shows a row of chapter buttons - one per holomem -
    above the page controls: green+disabled for the chapter being viewed, blue for a finished/ongoing
    chapter you can jump to, grey+disabled for a chapter that has not started yet."""

    def __init__(
        self,
        *,
        rows: list[dict],
        title: str,
        thumb: str | None = None,
        restrict_to: int,
        per_page: int = PER_PAGE,
        chapters: list[Chapter] | None = None,
        current_chapter: str | None = None,
        fetch: Fetch | None = None,
    ) -> None:
        super().__init__(timeout=180, restrict_to=restrict_to)
        self.entries = [_to_row(r) for r in rows]
        self.title = title
        self.thumb = thumb
        self.per_page = per_page
        self.page = 0
        self.mobile = False
        self.offset = False
        self.total_pages = max(1, math.ceil(len(self.entries) / per_page))
        # only worth a chapter row when there's more than one chapter to switch between
        self._chapters = chapters if chapters and len(chapters) > 1 else []
        self.current_chapter = current_chapter
        self._fetch = fetch
        self._chapter_items: list[tuple[discord.ui.Button, str, bool]] = []
        if self._chapters:
            self._build_chapter_buttons()
        self._update()

    # --- chapter buttons ---

    def _build_chapter_buttons(self) -> None:
        # the decorator page buttons auto-occupy row 0; free them first so the chapter rows can use
        # row 0, then re-add them below the chapters (moving a button's row needs a remove+add so the
        # view's row-weight bookkeeping stays correct)
        page_btns = [self.prev, self.next, self.mobile_btn, self.offset_btn]
        for b in page_btns:
            self.remove_item(b)
        shown = self._chapters[: _MAX_CHAPTER_ROWS * 5]
        for i, (cid, label, started, char_id) in enumerate(shown):
            btn = discord.ui.Button(
                label=label[:80] or "?", emoji=fanmark_emoji(char_id), row=i // 5
            )
            self._style_chapter(btn, cid, started)

            async def cb(interaction: discord.Interaction, cid=cid) -> None:
                await self._switch_chapter(interaction, cid)

            btn.callback = cb  # type: ignore[method-assign]
            self.add_item(btn)
            self._chapter_items.append((btn, cid, started))
        # page controls on their own row, right after the chapter rows
        page_row = min(math.ceil(len(shown) / 5), _MAX_CHAPTER_ROWS)
        for b in page_btns:
            b.row = page_row
            self.add_item(b)

    def _style_chapter(self, btn: discord.ui.Button, cid: str, started: bool) -> None:
        if cid == self.current_chapter:
            btn.style = discord.ButtonStyle.success  # green: you're on this page
            btn.disabled = True
        elif not started:
            btn.style = discord.ButtonStyle.secondary  # grey: not started yet
            btn.disabled = True
        else:
            btn.style = discord.ButtonStyle.primary  # blue: jump to it
            btn.disabled = False

    def _restyle_chapters(self) -> None:
        for btn, cid, started in self._chapter_items:
            self._style_chapter(btn, cid, started)

    async def _switch_chapter(self, interaction: discord.Interaction, cid: str) -> None:
        if cid == self.current_chapter or self._fetch is None:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        rows = await self._fetch(cid)
        self.entries = [_to_row(r) for r in rows]
        self.total_pages = max(1, math.ceil(len(self.entries) / self.per_page))
        self.page = 0
        self.offset = False
        self.current_chapter = cid
        self._restyle_chapters()
        self._update()
        await interaction.edit_original_response(embed=self._embed(), view=self)

    # --- rendering ---

    def _update(self) -> None:
        self.mobile_btn.style = (
            discord.ButtonStyle.primary if self.mobile else discord.ButtonStyle.secondary
        )
        self.offset_btn.style = (
            discord.ButtonStyle.primary if self.offset else discord.ButtonStyle.secondary
        )

    def _chapter_label(self) -> str | None:
        return next((l for c, l, *_ in self._chapters if c == self.current_chapter), None)

    def _window(self) -> list[LBRow]:
        total = len(self.entries)
        if not total:
            return []
        # OFFSET shifts the window half a page so a player near a boundary can see their neighbours;
        # the modulo wraps it around the whole board (mirrors RoboNene / sbuga)
        start = (self.page * self.per_page + (10 if self.offset else 0)) % total
        return [self.entries[(start + i) % total] for i in range(min(self.per_page, total))]

    def _embed(self) -> discord.Embed:
        title = self.title
        label = self._chapter_label()
        if label:
            title = f"{title} ({label})"
        embed = embeds.embed(title=title)
        if self.entries:
            embed.description = format_leaderboard(self._window(), mobile=self.mobile)
        else:
            embed.description = "No ranking data for this chapter yet."
        if self.thumb:
            embed.set_thumbnail(url=self.thumb)
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages}")
        return embed

    async def send_initial(self, interaction: discord.Interaction) -> None:
        self.message = await interaction.followup.send(embed=self._embed(), view=self, wait=True)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._update()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(emoji="⬅️", label="PREV", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = (self.page - 1) % self.total_pages
        await self._refresh(interaction)

    @discord.ui.button(emoji="➡️", label="NEXT", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = (self.page + 1) % self.total_pages
        await self._refresh(interaction)

    @discord.ui.button(emoji="📲", label="MOBILE", style=discord.ButtonStyle.secondary)
    async def mobile_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.mobile = not self.mobile
        await self._refresh(interaction)

    @discord.ui.button(emoji="🔃", label="OFFSET", style=discord.ButtonStyle.secondary)
    async def offset_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.offset = not self.offset
        await self._refresh(interaction)
