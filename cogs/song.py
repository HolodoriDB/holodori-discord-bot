from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import autocompletes
from helpers.views import Paginator
from services.holodori import HolodoriError, HolodoriNotFound

if TYPE_CHECKING:
    from main import HolodoriBot

_DIFF_ORDER = ["easy", "normal", "hard", "expert"]


def _fmt_length(seconds: int | None) -> str:
    if not seconds:
        return "—"
    return f"{seconds // 60}:{seconds % 60:02d}"


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

    @song.command(name="jacket", description="Show a song's jacket art.")
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

    @song.command(name="info", description="View a song's details and charts.")
    @app_commands.describe(song="Song title.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def info(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo and self.bot.data
        lang = await self._lang(interaction.user.id)
        try:
            detail = await self.bot.holo.get_song(song, lang)
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

        summary = self.bot.data.get_song(song)
        lines = []
        if detail.composer:
            lines.append(f"**Composer:** {detail.composer}")
        if detail.lyricist:
            lines.append(f"**Lyricist:** {detail.lyricist}")
        if detail.arranger:
            lines.append(f"**Arranger:** {detail.arranger}")
        if detail.characterGroupDisplayName:
            lines.append(f"**Singers:** {detail.characterGroupDisplayName}")
        lines.append(f"**Length:** {_fmt_length(detail.playingSeconds)}")
        if detail.obtain and detail.obtain.get("type"):
            lines.append(f"**Obtain:** {detail.obtain['type'].title()}")

        embed = embeds.embed(title=detail.title, description="\n".join(lines))
        # charts: level + note count per difficulty
        chart_lines = []
        for d in sorted(detail.difficulties, key=lambda x: _diff_key(x.difficultyType)):
            tname = _diff_type(d.difficultyType).title()
            notes = f" · {d.fullComboNoteCount} notes" if d.fullComboNoteCount else ""
            chart_lines.append(f"**{tname}** Lv.{d.difficultyLevel}{notes}")
        if chart_lines:
            embed.add_field(name="Charts", value="\n".join(chart_lines), inline=False)
        if summary and summary.jacket:
            embed.set_thumbnail(url=self.bot.holo.image_url(summary.jacket))
        embed.set_footer(text=f"ID: {detail.id}")
        await interaction.followup.send(embed=embed)

    @song.command(name="difficulty", description="List songs by a chart level.")
    @app_commands.describe(level="Chart level.", difficulty="Difficulty tier.")
    @app_commands.autocomplete(difficulty=autocompletes.choices(_DIFF_ORDER))
    async def difficulty(
        self, interaction: discord.Interaction, level: int, difficulty: str = "expert"
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        difficulty = difficulty.lower().strip()
        matches = []
        for s in self.bot.data.songs():
            for d in s.difficulties:
                if d.type == difficulty and d.level == level:
                    matches.append(s.title)
        if not matches:
            await interaction.followup.send(
                embed=embeds.error_embed(f"No **{difficulty}** charts at level {level}.")
            )
            return
        matches.sort()
        per_page = 20
        pages = [matches[i : i + per_page] for i in range(0, len(matches), per_page)]

        def render(page: int) -> discord.Embed:
            chunk = pages[page - 1]
            return embeds.embed(
                title=f"{difficulty.title()} · Level {level}",
                description="\n".join(f"• {t}" for t in chunk),
            ).set_footer(text=f"{len(matches)} songs · page {page}/{len(pages)}")

        if len(pages) == 1:
            await interaction.followup.send(embed=render(1))
            return
        view = Paginator(render, len(pages), interaction.user.id)
        await interaction.followup.send(embed=render(1), view=view)
        view.message = await interaction.original_response()


def _diff_type(difficulty_type: str) -> str:
    # "MusicDifficultyType_MUSIC_DIFFICULTY_TYPE_EASY" -> "easy"
    return difficulty_type.rsplit("_", 1)[-1].lower()


def _diff_key(difficulty_type: str) -> int:
    t = _diff_type(difficulty_type)
    return _DIFF_ORDER.index(t) if t in _DIFF_ORDER else 99


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(SongCog(bot))
