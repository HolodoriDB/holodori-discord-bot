from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import details, embeds, imaging, timezones
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS, autocompletes
from services.graph import render_graph
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

_TIER = (90, 150, 255, 255)
_USER = (120, 200, 130, 255)


class GraphCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    graph = app_commands.Group(
        name="graph",
        description="Event cutoff graphs.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _region(self, user_id: int, region: str) -> str:
        assert self.bot.user_data
        if region == "default":
            return await self.bot.user_data.get_settings(user_id, "default_region")
        return region

    @graph.command(name="cutoff", description="Graph a tier's cutoff over the event.")
    @app_commands.describe(
        tier="Tier rank to graph (e.g. 100).",
        region="Game server region.",
        event="Event (defaults to the latest).",
        border="Use the tier border line.",
        timezone="Timezone for the time axis (defaults to your setting).",
    )
    @app_commands.choices(region=REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def cutoff(
        self,
        interaction: discord.Interaction,
        tier: int,
        region: str = "default",
        event: str | None = None,
        border: bool = False,
        timezone: str | None = None,
    ) -> None:
        assert self.bot.holo and self.bot.user_data and self.bot.data
        if timezone is not None and timezones.canonical(timezone) is None:
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"`{timezone}` isn't a valid timezone. Use a common one "
                    f"({timezones.COMMON}) or an IANA name like `Europe/Paris`."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        region = await self._region(interaction.user.id, region)
        tz_name = timezone or await self.bot.user_data.get_settings(interaction.user.id, "timezone")
        tz = timezones.resolve(tz_name)
        try:
            data = await self.bot.holo.get_event_graph(
                region, tier, event_id=event, border=border or None
            )
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch graph: {e.detail or e.status}")
            )
            return
        tier_series = data.get("tier") or []
        user_series = data.get("user") or []
        lines: list[tuple[str, list, tuple[int, int, int, int]]] = []
        if tier_series:
            lines.append((f"Tier {tier} Cutoff", tier_series, _TIER))
        if user_series:
            lines.append((str(data.get("name") or f"#{tier}"), user_series, _USER))
        if not lines:
            await interaction.followup.send(
                embed=embeds.error_embed("No graph data for that event/tier yet.")
            )
            return
        title = f"Tier {tier} Cutoff - {REGION_LABELS.get(region, region)}"
        img = await asyncio.to_thread(render_graph, lines, title, tz=tz)

        embed = embeds.embed(title=title)
        last_ms = max((int(p[0]) for _, s, _ in lines for p in s), default=0)
        if last_ms:
            embed.description = f"**Last Data Update:** <t:{last_ms // 1000}:R>"
        files = [discord.File(io.BytesIO(img), "graph.png")]
        logo_bytes = await self._event_logo(region, event)
        if logo_bytes:
            files.append(discord.File(io.BytesIO(logo_bytes), "logo.png"))
            embed.set_thumbnail(url="attachment://logo.png")
        embed.set_image(url="attachment://graph.png")
        await interaction.followup.send(embed=embed, files=files)

    async def _event_logo(self, region: str, event_id: str | None) -> bytes | None:
        assert self.bot.data and self.bot.holo
        try:
            events = await self.bot.data.events(region, self.bot.holo.lang)
        except HolodoriError:
            return None
        ev = next((e for e in events if e.eventId == event_id), events[0] if events else None)
        if not ev or not ev.logo:
            return None
        return await details.unsquished_bytes(self.bot, ev.logo, imaging.ASPECT_LOGO)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(GraphCog(bot))
