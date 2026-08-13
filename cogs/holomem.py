from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import details, embeds
from helpers.autocompletes import REGION_LABELS, REGIONS, autocompletes
from helpers.lb_view import LeaderboardView
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

_REGION_CHOICES = [app_commands.Choice(name=REGION_LABELS[r], value=r) for r in REGIONS]


class HolomemCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    holomem = app_commands.Group(
        name="holomem",
        description="Holomem (character) information.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @holomem.command(name="list", description="List all holomems by branch.")
    async def list_(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        embed = embeds.embed(title="Holomems")
        for g in self.bot.data.holomem_groups():
            names = ", ".join(m.name for m in g.members)
            embed.add_field(name=g.name, value=names[:1024] or "—", inline=False)
        await interaction.followup.send(embed=embed)

    @holomem.command(name="info", description="View a holomem's profile and stickers.")
    @app_commands.describe(holomem="Holomem name.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    async def info(self, interaction: discord.Interaction, holomem: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        found = details.find_member(self.bot.data.holomem_groups(), holomem)
        if not found:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that holomem. Pick one from the list.")
            )
            return
        await interaction.followup.send(embed=details.holomem_embed(self.bot, *found))

    @holomem.command(name="leaderboard", description="Live per-holomem rating rank.")
    @app_commands.describe(holomem="Holomem name.", region="Game server region.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    @app_commands.choices(region=_REGION_CHOICES)
    async def leaderboard(
        self, interaction: discord.Interaction, holomem: str, region: str = "us"
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data and self.bot.holo
        member = self.bot.data.get_holomem(holomem)
        try:
            data = await self.bot.holo.get_holomem_leaderboard(region, holomem)
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch leaderboard: {e.detail or e.status}")
            )
            return
        rows = data.get("rows", [])
        if not rows:
            await interaction.followup.send(
                embed=embeds.error_embed("No leaderboard data for that holomem right now.")
            )
            return
        name = member.name if member else holomem
        title = f"{name} - {REGION_LABELS.get(region, region)} Rating"
        thumb = self.bot.holo.image_url(member.icon) if member and member.icon else None
        view = LeaderboardView(rows=rows, title=title, thumb=thumb, restrict_to=interaction.user.id)
        await view.send_initial(interaction)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(HolomemCog(bot))
