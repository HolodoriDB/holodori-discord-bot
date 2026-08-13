from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import EventInfo
from helpers import embeds
from helpers.autocompletes import REGIONS, autocompletes
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

_REGION_LABELS = {"us": "Global", "as": "Asia", "jp": "Japan"}
_REGION_CHOICES = [app_commands.Choice(name=_REGION_LABELS[r], value=r) for r in REGIONS]


def _ts(ms: int | None, style: str = "F") -> str:
    return f"<t:{ms // 1000}:{style}>" if ms else "—"


class EventsCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    event = app_commands.Group(
        name="event",
        description="Event information.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")

    async def _events(self, region: str, user_id: int) -> list[EventInfo]:
        assert self.bot.data
        return await self.bot.data.events(region, await self._lang(user_id))

    def _info_embed(self, ev: EventInfo) -> discord.Embed:
        assert self.bot.holo
        status = "🟢 Live" if ev.live else "⚫ Ended"
        lines = [
            f"**Status:** {status}",
            f"**Type:** {'Song Score' if ev.isSongScore else 'Marathon'}",
            f"**Starts:** {_ts(ev.startTime)}",
            f"**Ends:** {_ts(ev.endTime)}",
            f"**Results:** {_ts(ev.revealStartTime)}",
        ]
        embed = embeds.embed(title=ev.name, description="\n".join(lines))
        if ev.logo:
            embed.set_thumbnail(url=self.bot.holo.image_url(ev.logo))
        if ev.banner:
            embed.set_image(url=self.bot.holo.image_url(ev.banner))
        embed.set_footer(text=f"ID: {ev.eventId}")
        return embed

    @event.command(name="info", description="View an event's details.")
    @app_commands.describe(region="Game server region.", event="Event (defaults to the latest).")
    @app_commands.choices(region=_REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def info(
        self,
        interaction: discord.Interaction,
        region: str = "us",
        event: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            events = await self._events(region, interaction.user.id)
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch events: {e.detail or e.status}")
            )
            return
        if not events:
            await interaction.followup.send(embed=embeds.error_embed("No events found."))
            return
        ev = next((e for e in events if e.eventId == event), events[0]) if event else events[0]
        await interaction.followup.send(embed=self._info_embed(ev))

    @event.command(name="schedule", description="Current and upcoming events.")
    @app_commands.describe(region="Game server region.")
    @app_commands.choices(region=_REGION_CHOICES)
    async def schedule(self, interaction: discord.Interaction, region: str = "us") -> None:
        await interaction.response.defer(thinking=True)
        try:
            events = await self._events(region, interaction.user.id)
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch events: {e.detail or e.status}")
            )
            return
        if not events:
            await interaction.followup.send(embed=embeds.error_embed("No events found."))
            return
        embed = embeds.embed(title=f"{_REGION_LABELS.get(region, region)} Event Schedule")
        for ev in events[:8]:
            status = "🟢" if ev.live else "⚫"
            embed.add_field(
                name=f"{status} {ev.name}",
                value=f"{_ts(ev.startTime, 'd')} → {_ts(ev.endTime, 'd')}",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @event.command(name="leaderboard", description="Top rankings for an event.")
    @app_commands.describe(region="Game server region.", event="Event (defaults to the latest).")
    @app_commands.choices(region=_REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        region: str = "us",
        event: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.holo
        try:
            data = await self.bot.holo.get_event_leaderboard(
                region, event_id=event, language=await self._lang(interaction.user.id)
            )
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch leaderboard: {e.detail or e.status}")
            )
            return
        rankings = data.get("rankings", [])
        if not rankings:
            await interaction.followup.send(
                embed=embeds.error_embed("No ranking data for that event yet.")
            )
            return
        lines = [
            f"**#{r['rank']}** {discord.utils.escape_markdown(str(r.get('name', '?')))} — "
            f"{int(r.get('score', 0)):,}"
            for r in rankings[:20]
        ]
        embed = embeds.embed(
            title=f"{data.get('eventName', 'Event')} · {_REGION_LABELS.get(region, region)}",
            description="\n".join(lines),
        )
        borders = data.get("borders", [])
        if borders:
            embed.add_field(
                name="Borders",
                value="\n".join(
                    f"T{b['rank']}: {int(b.get('score', 0)):,}" for b in borders[:10]
                ),
                inline=False,
            )
        if data.get("logo"):
            embed.set_thumbnail(url=self.bot.holo.image_url(data["logo"]))
        await interaction.followup.send(embed=embed)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(EventsCog(bot))
