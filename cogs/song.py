from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands

from data.search import preprocess
from helpers import details, embeds, imaging
from helpers.aliases import alias_field
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
        s = self.bot.data.match_song_aliased(song)
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
        s = self.bot.data.match_song_aliased(song)
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
            chart_bytes = await asyncio.to_thread(imaging.mirror_chart, chart_bytes)
            embed.description += "\n\n**MIRRORED CHART**"
        embed.set_image(url="attachment://chart.png")
        # cdn always has the un-mirrored chart; the api route mirrors on demand (browser can solve
        # any cloudflare challenge itself, so the link needs no bypass header)
        if mirror:
            button_url = (
                f"{self.bot.holo.base}/api/chart?music_id={quote(s.id)}"
                f"&difficulty={difficulty}&mirror=1"
            )
        else:
            button_url = url
        view = LinkButtonView([("Chart Image", button_url)])
        view.message = await interaction.followup.send(
            embed=embed, file=discord.File(io.BytesIO(chart_bytes), "chart.png"), view=view, wait=True
        )

    @song.command(name="info", description="View a song's data.")
    @app_commands.describe(song="Song title.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def info(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo and self.bot.data
        s = self.bot.data.match_song_aliased(song)
        if not s:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        try:
            detail = await self.bot.holo.get_song(s.id, await self._lang(interaction.user.id))
        except HolodoriNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch song: {e.detail or e.status}")
            )
            return
        await interaction.followup.send(embed=details.song_embed(self.bot, detail))

    @song.command(name="aliases", description="Every name/romanization/alias this song is found by.")
    @app_commands.describe(song="Song title.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def aliases(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        s = self.bot.data.match_song_aliased(song)
        if not s:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return
        manual = sorted(self.bot.data.song_aliases(s.id))
        # the keys the matcher accepts, minus the manual aliases, the title, and the bare id
        skip = {preprocess(a) for a in manual} | {preprocess(s.title), str(s.id)}
        auto = [k for k in self.bot.data.song_search_keys(s.id) if k not in skip]
        embed = embeds.embed(
            title="Aliases",
            description=f"Aliases for `{s.title}` (ID `{s.id}`)",
        )
        embed.add_field(name="Manually Added", value=alias_field(manual), inline=False)
        embed.add_field(name="Automatically Generated", value=alias_field(auto), inline=False)
        await interaction.followup.send(embed=embed)

    @song.command(name="difficulty", description="Find all songs of a level.")
    @app_commands.describe(level="Level to search (1-40).")
    async def difficulty(self, interaction: discord.Interaction, level: int) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        if not 1 <= level <= 40:
            await interaction.followup.send(
                embed=embeds.error_embed("Level must be between 1 and 40.")
            )
            return
        order = {d: i for i, d in enumerate(reversed(_DIFFS))}  # expert first
        found: list[tuple[str, str]] = []
        for s in self.bot.data.songs():
            for d in s.difficulties:
                if d.level == level:
                    found.append((d.type, s.title))
        if not found:
            await interaction.followup.send(
                embed=embeds.error_embed(f"No charts found at level {level}.")
            )
            return
        found.sort(key=lambda x: (order.get(x[0], 99), x[1].lower()))
        lines = [f"**{diff.title()}** - {title}" for diff, title in found]
        per_page = 25
        pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)]

        def render(page: int) -> discord.Embed:
            embed = embeds.embed(title=f"Level {level} Songs", color=discord.Color.blue())
            embed.description = "\n".join(pages[page - 1]) + f"\n\n-# Page {page}/{len(pages)}"
            return embed

        if len(pages) == 1:
            await interaction.followup.send(embed=render(1))
            return
        view = Paginator(render, len(pages), interaction.user.id)
        view.message = await interaction.followup.send(embed=render(1), view=view, wait=True)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(SongCog(bot))
