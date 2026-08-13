from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import HolomemGroup, HolomemMember
from helpers import embeds
from helpers.autocompletes import REGIONS, autocompletes
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

_REGION_LABELS = {"us": "Global", "as": "Asia", "jp": "Japan"}


def _find_member(
    groups: list[HolomemGroup], holomem_id: str
) -> tuple[HolomemGroup, HolomemMember] | None:
    for g in groups:
        for m in g.members:
            if m.id == holomem_id:
                return g, m
    return None


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

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")

    @holomem.command(name="list", description="List all holomems by branch.")
    async def list_(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        groups = self.bot.data.holomem_groups()
        embed = embeds.embed(title="Holomems")
        for g in groups:
            names = ", ".join(m.name for m in g.members)
            embed.add_field(name=g.name, value=names[:1024] or "—", inline=False)
        await interaction.followup.send(embed=embed)

    @holomem.command(name="info", description="View a holomem's profile and stickers.")
    @app_commands.describe(holomem="Holomem name.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    async def info(self, interaction: discord.Interaction, holomem: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data and self.bot.holo
        groups = self.bot.data.holomem_groups()
        found = _find_member(groups, holomem)
        if not found:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that holomem. Pick one from the list.")
            )
            return
        group, member = found
        lines = [f"**Branch:** {group.name}"]
        if member.stickers:
            lines.append(f"**Membership Stickers:** {len(member.stickers)}")
        embed = embeds.embed(title=member.name, description="\n".join(lines))
        icon = self.bot.holo.image_url(member.icon)
        if icon:
            embed.set_thumbnail(url=icon)
        if member.stickers and member.stickers[0].image:
            embed.set_image(url=self.bot.holo.image_url(member.stickers[0].image))
        embed.set_footer(text=f"ID: {member.id}")
        await interaction.followup.send(embed=embed)

    @holomem.command(name="leaderboard", description="Live per-holomem rating rank.")
    @app_commands.describe(holomem="Holomem name.", region="Game server region.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    @app_commands.choices(
        region=[app_commands.Choice(name=_REGION_LABELS[r], value=r) for r in REGIONS]
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        holomem: str,
        region: str = "us",
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
        lines = [
            f"**#{r['rank']}** {discord.utils.escape_markdown(str(r.get('name', '?')))} — "
            f"{int(r.get('score', 0)):,}"
            for r in rows[:20]
        ]
        title = f"{member.name if member else holomem} · {_REGION_LABELS.get(region, region)} Rating"
        embed = embeds.embed(title=title, description="\n".join(lines))
        if member:
            icon = self.bot.holo.image_url(member.icon)
            if icon:
                embed.set_thumbnail(url=icon)
        await interaction.followup.send(embed=embed)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(HolomemCog(bot))
