from __future__ import annotations

import math

import discord

from helpers import embeds
from helpers.views import HoloView
from services.leaderboard import LBRow, format_leaderboard

PER_PAGE = 20


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
    toggle that narrows the table. no image, so Discord renders every glyph (CN/JP names included)."""

    def __init__(
        self,
        *,
        rows: list[dict],
        title: str,
        thumb: str | None = None,
        restrict_to: int,
        per_page: int = PER_PAGE,
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
        self._update()

    def _update(self) -> None:
        self.mobile_btn.style = (
            discord.ButtonStyle.primary if self.mobile else discord.ButtonStyle.secondary
        )
        self.offset_btn.style = (
            discord.ButtonStyle.primary if self.offset else discord.ButtonStyle.secondary
        )

    def _window(self) -> list[LBRow]:
        total = len(self.entries)
        if not total:
            return []
        # OFFSET shifts the window half a page so a player near a boundary can see their neighbours;
        # the modulo wraps it around the whole board (mirrors RoboNene / sbuga)
        start = (self.page * self.per_page + (10 if self.offset else 0)) % total
        return [self.entries[(start + i) % total] for i in range(min(self.per_page, total))]

    def _embed(self) -> discord.Embed:
        embed = embeds.embed(title=self.title)
        embed.description = format_leaderboard(self._window(), mobile=self.mobile)
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
