from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import EventInfo
from helpers import details, embeds
from helpers.autocompletes import REGION_LABELS, REGIONS, autocompletes
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

_REGION_CHOICES = [app_commands.Choice(name=REGION_LABELS[r], value=r) for r in REGIONS]


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
        embed, files = await details.event_embed(self.bot, ev)
        await interaction.followup.send(embed=embed, files=files)

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
        embed = embeds.embed(title=f"{REGION_LABELS.get(region, region)} Event Schedule")
        for ev in events[:8]:
            marker = "⚫" if details.event_ended(ev) else "🟢"
            end = _ts(ev.endTime, "d") if ev.endTime else "No end date"
            embed.add_field(
                name=f"{marker} {ev.name}",
                value=f"{_ts(ev.startTime, 'd')} → {end}",
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
        from helpers.lb_view import LeaderboardView

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
        title = f"{data.get('eventName', 'Event')} - {REGION_LABELS.get(region, region)}"
        thumb = self.bot.holo.image_url(data["logo"]) if data.get("logo") else None
        view = LeaderboardView(
            rows=rankings, title=title, thumb=thumb, restrict_to=interaction.user.id
        )
        await view.send_initial(interaction)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(EventsCog(bot))
