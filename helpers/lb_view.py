from __future__ import annotations

import asyncio
import io
import math

import discord

from helpers import embeds
from helpers.views import HoloView
from services.leaderboard import LBRow, render_leaderboard

PER_PAGE = 20


class LeaderboardView(HoloView):
    """image-rendered leaderboard: prev/next pages + an OFFSET toggle for the rank/score change
    columns. no ALT columns, no games-per-hour."""

    def __init__(
        self,
        *,
        rows: list[dict],
        title: str,
        thumb: str | None = None,
        thumb_bytes: bytes | None = None,
        restrict_to: int,
        per_page: int = PER_PAGE,
    ) -> None:
        super().__init__(timeout=180, restrict_to=restrict_to)
        self.rows = rows
        self.title = title
        self.thumb = thumb
        self.thumb_bytes = thumb_bytes
        self.per_page = per_page
        self.page = 1
        self.offset = False
        self.total_pages = max(1, math.ceil(len(rows) / per_page))
        self._update()

    def _update(self) -> None:
        self.prev.disabled = self.page == 1
        self.next.disabled = self.page == self.total_pages
        self.toggle.label = "Hide Change" if self.offset else "Show Change"

    def _lbrows(self) -> list[LBRow]:
        chunk = self.rows[(self.page - 1) * self.per_page : self.page * self.per_page]
        out: list[LBRow] = []
        for r in chunk:
            rank = f"#{r.get('rank', '?')}"
            score = f"{int(r.get('score', 0)):,}"
            name = str(r.get("name", "?"))
            if self.offset:
                rc = int(r.get("rankChange") or 0)
                ep = int(r.get("epChange") or 0)
                change = f"+{ep:,}" if ep > 0 else (f"{ep:,}" if ep else "-")
                out.append(LBRow(rank, name, [score, change], 1 if rc > 0 else (-1 if rc < 0 else 0), abs(rc)))
            else:
                out.append(LBRow(rank, name, [score]))
        return out

    def _render(self) -> bytes:
        columns = ["Score", "Change"] if self.offset else ["Score"]
        return render_leaderboard(self._lbrows(), columns, show_delta=self.offset)

    def _files(self, img: bytes) -> list[discord.File]:
        files = [discord.File(io.BytesIO(img), "lb.png")]
        if self.thumb_bytes:
            files.append(discord.File(io.BytesIO(self.thumb_bytes), "thumb.png"))
        return files

    def _embed(self) -> discord.Embed:
        embed = embeds.embed(title=self.title)
        embed.set_image(url="attachment://lb.png")
        if self.thumb_bytes:
            embed.set_thumbnail(url="attachment://thumb.png")
        elif self.thumb:
            embed.set_thumbnail(url=self.thumb)
        embed.set_footer(text=f"Page {self.page}/{self.total_pages}")
        return embed

    async def send_initial(self, interaction: discord.Interaction) -> None:
        img = await asyncio.to_thread(self._render)
        self.message = await interaction.followup.send(
            embed=self._embed(), files=self._files(img), view=self, wait=True
        )

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._update()
        img = await asyncio.to_thread(self._render)
        await interaction.response.edit_message(
            embed=self._embed(), attachments=self._files(img), view=self
        )

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page > 1:
            self.page -= 1
        await self._refresh(interaction)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page < self.total_pages:
            self.page += 1
        await self._refresh(interaction)

    @discord.ui.button(label="Show Change", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.offset = not self.offset
        await self._refresh(interaction)
