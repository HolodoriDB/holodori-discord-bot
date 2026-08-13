from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import EventInfo
from helpers import details, embeds, imaging
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS, autocompletes

if TYPE_CHECKING:
    from main import HolodoriBot


def _ts(ms: int | None, style: str = "R") -> str:
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

    async def _region(self, user_id: int, region: str) -> str:
        assert self.bot.user_data
        if region == "default":
            return await self.bot.user_data.get_settings(user_id, "default_region")
        return region

    async def _lang(self, user_id: int) -> str:
        assert self.bot.user_data
        return await self.bot.user_data.get_settings(user_id, "default_language")

    async def _events(self, region: str, user_id: int) -> list[EventInfo]:
        assert self.bot.data
        return await self.bot.data.events(region, await self._lang(user_id))

    @event.command(name="info", description="View an event's details.")
    @app_commands.describe(event="Event (defaults to the latest).", region="Game server region.")
    @app_commands.choices(region=REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def info(
        self,
        interaction: discord.Interaction,
        event: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        region = await self._region(interaction.user.id, region)
        events = await self._events(region, interaction.user.id)
        if not events:
            await interaction.followup.send(embed=embeds.error_embed("No events found."))
            return
        if event:
            ev = next((e for e in events if e.eventId == event), None)
            if ev is None:
                await interaction.followup.send(
                    embed=embeds.error_embed("Couldn't find that event.")
                )
                return
        else:
            ev = events[0]
        embed, files = await details.event_embed(self.bot, ev)
        await interaction.followup.send(embed=embed, files=files)

    @event.command(name="schedule", description="View the current and next event.")
    @app_commands.describe(region="Game server region.")
    @app_commands.choices(region=REGION_CHOICES)
    async def schedule(self, interaction: discord.Interaction, region: str = "default") -> None:
        await interaction.response.defer(thinking=True)
        region = await self._region(interaction.user.id, region)
        events = await self._events(region, interaction.user.id)
        if not events:
            await interaction.followup.send(embed=embeds.error_embed("No events found."))
            return
        now = time.time() * 1000
        current = next((e for e in events if not details.event_ended(e) and (e.startTime or 0) <= now), None)
        upcoming = sorted(
            (e for e in events if e.startTime and e.startTime > now), key=lambda e: e.startTime or 0
        )
        nxt = upcoming[0] if upcoming else None

        embed = embeds.embed(title=f"{REGION_LABELS.get(region, region)} Event Schedule")
        if current:
            embed.add_field(
                name="Current Event",
                value=f"**{current.name}**\nEnds {_ts(current.endTime)}"
                if current.endTime
                else f"**{current.name}**\nNo end date",
                inline=False,
            )
        if nxt:
            embed.add_field(
                name="Next Event",
                value=f"**{nxt.name}**\nStarts {_ts(nxt.startTime)}",
                inline=False,
            )
        elif not current:
            embed.description = "No current or upcoming events."
        embed.set_footer(text="Times are shown in your local time.")
        await interaction.followup.send(embed=embed)

    @event.command(name="leaderboard", description="Top rankings for an event.")
    @app_commands.describe(event="Event (defaults to the latest).", region="Game server region.")
    @app_commands.choices(region=REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        event: str | None = None,
        region: str = "default",
    ) -> None:
        from helpers.lb_view import LeaderboardView

        await interaction.response.defer(thinking=True)
        assert self.bot.holo
        region = await self._region(interaction.user.id, region)
        data = await self.bot.holo.get_event_leaderboard(
            region, event_id=event, language=await self._lang(interaction.user.id)
        )
        rankings = data.get("rankings", [])
        if not rankings:
            await interaction.followup.send(
                embed=embeds.error_embed("No ranking data for that event yet.")
            )
            return
        title = f"{data.get('eventName', 'Event')} - Top 100"
        logo_bytes = await details.unsquished_bytes(self.bot, data.get("logo"), imaging.ASPECT_LOGO)
        view = LeaderboardView(
            rows=rankings, title=title, thumb_bytes=logo_bytes, restrict_to=interaction.user.id
        )
        await view.send_initial(interaction)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(EventsCog(bot))
