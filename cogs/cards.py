from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import CardDetail
from helpers import embeds
from helpers.autocompletes import autocompletes
from services.holodori import HolodoriError, HolodoriNotFound

if TYPE_CHECKING:
    from main import HolodoriBot

_ATTR_COLORS = {"Cute": 0xFF6FA5, "Pure": 0x5AC8E6, "Happy": 0xFFB13D}
_SKILL_LABELS = {"passive": "Passive", "active": "Active", "special": "Special"}
_HIGHLIGHT_RE = re.compile(r"\[/?highlight\]")
_OTHER_TAG_RE = re.compile(r"\[[^\]]+\]")


def clean_skill_text(text: str) -> str:
    text = _HIGHLIGHT_RE.sub("**", text)
    text = _OTHER_TAG_RE.sub("", text)
    return text.strip()


def _card_embed(bot: HolodoriBot, card: CardDetail) -> discord.Embed:
    assert bot.holo
    color = _ATTR_COLORS.get(card.attributeName or "", 0xE85D9E)
    stars = "★" * card.rarity
    group = card.group.name if card.group else "—"
    lines = [
        f"**Character:** {card.character}",
        f"**Rarity:** {stars}",
        f"**Attribute:** {card.attributeName or '—'}",
        f"**Main Stat:** {(card.mainStat or '—').title()}",
        f"**Group:** {group}",
    ]
    if card.baseTotals:
        lines.append(f"**Max Base Total:** {card.baseTotals[-1]:,}")
    embed = embeds.embed(title=card.name, description="\n".join(lines), color=discord.Color(color))
    for skill in card.skills:
        desc = clean_skill_text(skill.descriptions[-1]) if skill.descriptions else "—"
        label = _SKILL_LABELS.get(skill.type, skill.type.title())
        embed.add_field(name=f"{label} Skill", value=desc[:1024] or "—", inline=False)
    art = bot.holo.image_url(card.image)
    if art:
        embed.set_image(url=art)
    embed.set_footer(text=f"ID: {card.id}")
    return embed


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

    @card.command(name="info", description="View a card's art, stats, and skills.")
    @app_commands.describe(card="Card name.")
    @app_commands.autocomplete(card=autocompletes.card())
    async def info(self, interaction: discord.Interaction, card: str) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo and self.bot.data
        lang = await self._lang(interaction.user.id)
        try:
            detail = await self.bot.holo.get_card(card, lang)
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
        await interaction.followup.send(embed=_card_embed(self.bot, detail))

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(CardsCog(bot))
