from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import details, embeds, imaging
from helpers.autocompletes import autocompletes
from helpers.views import LinkButtonView, Paginator
from services.holodori import HolodoriError, HolodoriNotFound

if TYPE_CHECKING:
    from main import HolodoriBot

_DIFFS = ["easy", "normal", "hard", "expert"]
_DIFF_CHOICES = [app_commands.Choice(name=d.title(), value=d) for d in _DIFFS]


class SongCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    song = app_commands.Group(
        name="song",
        description="Song information.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")

    @song.command(name="jacket", description="View a song's jacket.")
    @app_commands.describe(song="Song title.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def jacket(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo and self.bot.data
        s = self.bot.data.get_song(song)
        if not s:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that song. Pick one from the list.")
            )
            return
        embed = embeds.embed(title=s.title)
        art = self.bot.holo.image_url(s.jacket)
        if art:
            embed.set_image(url=art)
        await interaction.followup.send(embed=embed)

    @song.command(name="chart", description="View a song's chart.")
    @app_commands.describe(
        song="Song title.",
        difficulty="Chart difficulty (defaults to your setting).",
        mirror="Show the mirrored chart (defaults to your setting).",
    )
    @app_commands.autocomplete(song=autocompletes.song())
    @app_commands.choices(difficulty=_DIFF_CHOICES)
    async def chart(
        self,
        interaction: discord.Interaction,
        song: str,
        difficulty: str | None = None,
        mirror: bool | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo and self.bot.data and self.bot.user_data
        s = self.bot.data.get_song(song)
        if not s:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that song. Pick one from the list.")
            )
            return
        if not difficulty:
            difficulty = await self.bot.user_data.get_settings(interaction.user.id, "default_difficulty")
        if mirror is None:
            mirror = await self.bot.user_data.get_settings(interaction.user.id, "mirror_charts_by_default")
        url = self.bot.holo.chart_image_url(s.id, difficulty)
        embed = embeds.embed(title=s.title, description=f"**Difficulty:** {difficulty.title()}")
        try:
            chart_bytes = await self.bot.holo.fetch_bytes(url)
        except Exception:
            embed.description = f"**{difficulty.title()}** doesn't exist for this song."
            embed.color = discord.Color.red()
            await interaction.followup.send(embed=embed)
            return
        if mirror:
            chart_bytes = await asyncio.to_thread(imaging.mirror, chart_bytes)
            embed.description += "\n\n**MIRRORED CHART**"
        embed.set_image(url="attachment://chart.png")
        view = LinkButtonView([("Chart Image", url)])
        view.message = await interaction.followup.send(
            embed=embed, file=discord.File(io.BytesIO(chart_bytes), "chart.png"), view=view, wait=True
        )

    @song.command(name="info", description="View a song's data.")
    @app_commands.describe(song="Song title.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def info(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo
        try:
            detail = await self.bot.holo.get_song(song, await self._lang(interaction.user.id))
        except HolodoriNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that song. Pick one from the list.")
            )
            return
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch song: {e.detail or e.status}")
            )
            return
        await interaction.followup.send(embed=details.song_embed(self.bot, detail))

    @song.command(name="difficulty", description="Find all songs of a level.")
    @app_commands.describe(level="Level to search.", difficulty="Difficulty tier.")
    @app_commands.choices(difficulty=_DIFF_CHOICES)
    async def difficulty(
        self, interaction: discord.Interaction, level: int, difficulty: str = "expert"
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        matches = sorted(
            s.title
            for s in self.bot.data.songs()
            if any(d.type == difficulty and d.level == level for d in s.difficulties)
        )
        if not matches:
            await interaction.followup.send(
                embed=embeds.error_embed(f"No **{difficulty.title()}** charts at level {level}.")
            )
            return
        per_page = 20
        pages = [matches[i : i + per_page] for i in range(0, len(matches), per_page)]

        def render(page: int) -> discord.Embed:
            embed = embeds.embed(
                title=f"{difficulty.title()} - Level {level}",
                description="\n".join(f"• {t}" for t in pages[page - 1]),
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"{len(matches)} songs - page {page}/{len(pages)}")
            return embed

        if len(pages) == 1:
            await interaction.followup.send(embed=render(1))
            return
        view = Paginator(render, len(pages), interaction.user.id)
        view.message = await interaction.followup.send(embed=render(1), view=view, wait=True)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(SongCog(bot))
