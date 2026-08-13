from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import details, embeds
from helpers.autocompletes import autocompletes
from services.holodori import HolodoriError, HolodoriNotFound

if TYPE_CHECKING:
    from main import HolodoriBot


class CardsCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    card = app_commands.Group(
        name="card",
        description="Card information.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")

    @card.command(name="info", description="View a card's art, stats, and skills.")
    @app_commands.describe(card="Card name.")
    @app_commands.autocomplete(card=autocompletes.card())
    async def info(self, interaction: discord.Interaction, card: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo
        try:
            detail = await self.bot.holo.get_card(card, await self._lang(interaction.user.id))
        except HolodoriNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that card. Pick one from the list.")
            )
            return
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch card: {e.detail or e.status}")
            )
            return
        embed, files = await details.card_embed(self.bot, detail)
        await interaction.followup.send(embed=embed, files=files)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(CardsCog(bot))
