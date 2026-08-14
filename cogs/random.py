from __future__ import annotations

import asyncio
import random as rnd
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import Song, SongDetail
from helpers import embeds

if TYPE_CHECKING:
    from main import HolodoriBot

_DIFFS = ["easy", "normal", "hard", "expert"]
_DIFF_CHOICES = [app_commands.Choice(name=d.title(), value=d) for d in _DIFFS]


class RandomCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    random = app_commands.Group(
        name="random",
        description="Get a random pick.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")

    def _song_embed(self, s: Song, diff: str, detail: SongDetail | None) -> discord.Embed:
        assert self.bot.holo
        # level from the summary chart; note count only exists on the fetched detail
        level = next((d.level for d in s.difficulties if d.type == diff), None)
        notes: int | None = None
        if detail:
            md = next(
                (d for d in detail.difficulties if d.difficultyType.rsplit("_", 1)[-1].lower() == diff),
                None,
            )
            if md:
                level = md.difficultyLevel
                notes = md.fullComboNoteCount
        lines = [f"**{diff.title()} {level}**" + (f" ({notes:,} notes)" if notes else "")]
        if detail:
            if detail.characterGroupDisplayName:
                lines.append(f"**Singer:** {detail.characterGroupDisplayName}")
            if detail.composer and detail.composer != "-":
                lines.append(f"**Composer:** {detail.composer}")
        embed = embeds.embed(title=s.title, description="\n".join(lines))
        if s.jacket:
            embed.set_thumbnail(url=self.bot.holo.image_url(s.jacket))
        return embed

    @random.command(
        name="song",
        description="Roll one or more random songs, optionally filtered by difficulty and level.",
    )
    @app_commands.describe(
        difficulty="Only consider this chart difficulty.",
        min="Minimum chart level (1-40).",
        max="Maximum chart level (1-40).",
        amount="How many songs to roll (1-10).",
    )
    @app_commands.choices(difficulty=_DIFF_CHOICES)
    async def song(
        self,
        interaction: discord.Interaction,
        difficulty: str | None = None,
        min: app_commands.Range[int, 1, 40] | None = None,
        max: app_commands.Range[int, 1, 40] | None = None,
        amount: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data and self.bot.holo
        lo, hi = (min or 1), (max or 40)
        if lo > hi:
            await interaction.followup.send(
                embed=embeds.error_embed("`min` can't be greater than `max`.")
            )
            return

        now = time.time() * 1000

        def picks_for(s: Song) -> list[str]:
            if s.startTime and s.startTime > now:  # unreleased
                return []
            return [
                d.type
                for d in s.difficulties
                if (not difficulty or d.type == difficulty) and lo <= d.level <= hi
            ]

        pool = [s for s in self.bot.data.songs() if picks_for(s)]
        if not pool:
            crit = []
            if difficulty:
                crit.append(f"a {difficulty.title()} chart")
            if min is not None or max is not None:
                crit.append(f"a level between {lo} and {hi}")
            extra = f" with {' and '.join(crit)}" if crit else ""
            await interaction.followup.send(embed=embeds.error_embed(f"No songs found{extra}."))
            return

        k = amount if amount <= len(pool) else len(pool)
        # each pick settles on one difficulty: the requested one, else a random qualifying chart
        chosen = [(s, difficulty or rnd.choice(picks_for(s))) for s in rnd.sample(pool, k)]
        lang = await self._lang(interaction.user.id)
        fetched = await asyncio.gather(
            *(self.bot.holo.get_song(s.id, lang) for s, _ in chosen), return_exceptions=True
        )
        out = [
            self._song_embed(s, diff, det if isinstance(det, SongDetail) else None)
            for (s, diff), det in zip(chosen, fetched)
        ]
        plural = "s" if len(out) > 1 else ""
        await interaction.followup.send(content=f"🎲 Your random song{plural}:", embeds=out)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(RandomCog(bot))
