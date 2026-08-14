from __future__ import annotations

import random as rnd
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import details, embeds
from services.holodori import HolodoriError, HolodoriNotFound

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

    @random.command(name="song", description="Get a random song, optionally filtered by difficulty and level.")
    @app_commands.describe(
        difficulty="Only consider this chart difficulty.",
        min="Minimum chart level (1-40).",
        max="Maximum chart level (1-40).",
    )
    @app_commands.choices(difficulty=_DIFF_CHOICES)
    async def song(
        self,
        interaction: discord.Interaction,
        difficulty: str | None = None,
        min: app_commands.Range[int, 1, 40] | None = None,
        max: app_commands.Range[int, 1, 40] | None = None,
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

        def qualifies(s) -> bool:
            if s.startTime and s.startTime > now:  # unreleased
                return False
            diffs = [d for d in s.difficulties if not difficulty or d.type == difficulty]
            return any(lo <= d.level <= hi for d in diffs)

        pool = [s for s in self.bot.data.songs() if qualifies(s)]
        if not pool:
            crit = []
            if difficulty:
                crit.append(f"a {difficulty.title()} chart")
            if min is not None or max is not None:
                crit.append(f"a level between {lo} and {hi}")
            extra = f" with {' and '.join(crit)}" if crit else ""
            await interaction.followup.send(embed=embeds.error_embed(f"No songs found{extra}."))
            return

        s = rnd.choice(pool)
        try:
            detail = await self.bot.holo.get_song(s.id, await self._lang(interaction.user.id))
        except (HolodoriError, HolodoriNotFound):
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't fetch that song, please try again.")
            )
            return
        await interaction.followup.send(content="🎲 Your random song:", embed=details.song_embed(self.bot, detail))


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(RandomCog(bot))
