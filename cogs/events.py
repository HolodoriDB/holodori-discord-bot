from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import EventInfo
from data.search import preprocess
from helpers import details, embeds, text_commands
from helpers.aliases import alias_field
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS, autocompletes

if TYPE_CHECKING:
    from helpers.lb_view import LeaderboardView
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
        assert self.bot.data
        region = await self._region(interaction.user.id, region)
        events = await self._events(region, interaction.user.id)
        if not events:
            await interaction.followup.send(embed=embeds.error_embed("No events found."))
            return
        if event:
            ev = self.bot.data.match_event_aliased(events, event)
            if ev is None:
                await interaction.followup.send(
                    embed=embeds.error_embed("Couldn't find that event.")
                )
                return
        else:
            ev = events[0]
        embed, files = await details.event_embed(self.bot, ev)
        await interaction.followup.send(embed=embed, files=files)

    @event.command(name="aliases", description="Every name/romanization/alias this event is found by.")
    @app_commands.describe(event="Event.", region="Game server region.")
    @app_commands.choices(region=REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def aliases(
        self, interaction: discord.Interaction, event: str, region: str = "default"
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.data
        region = await self._region(interaction.user.id, region)
        events = await self._events(region, interaction.user.id)
        ev = self.bot.data.match_event_aliased(events, event)
        if ev is None:
            await interaction.followup.send(embed=embeds.error_embed("Couldn't find that event."))
            return
        manual = sorted(self.bot.data.event_aliases(ev.eventId))
        # the keys the matcher accepts, minus the manual aliases, the generic label, and the bare id
        skip = {preprocess(a) for a in manual} | {preprocess(ev.name), str(ev.eventId)}
        auto = [k for k in self.bot.data.event_search_keys(ev.eventId) if k not in skip]
        embed = embeds.embed(
            title="Aliases",
            description=f"Aliases for event `{ev.eventId}`",
        )
        embed.add_field(name="Manually Added", value=alias_field(manual), inline=False)
        embed.add_field(name="Automatically Generated", value=alias_field(auto), inline=False)
        await interaction.followup.send(embed=embed)

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

    async def _leaderboard_view(
        self, *, user_id: int, region: str, event: str | None = None
    ) -> tuple["LeaderboardView | None", discord.Embed | None]:
        # the shared body of `/event leaderboard` and `%leaderboard`: (view, None) or (None, error)
        from helpers.chapter_buttons import chapters_from_event
        from helpers.lb_view import LeaderboardView

        assert self.bot.holo
        region = await self._region(user_id, region)
        lang = await self._lang(user_id)
        data = await self.bot.holo.get_event_leaderboard(region, event_id=event, language=lang)
        rankings = data.get("rankings", [])
        if not rankings:
            return None, embeds.error_embed("No ranking data for that event yet.")
        eid = data.get("eventId")
        current_chapter = data.get("chapterId")

        # relay events get a row of holomem chapter buttons; find the served event's chapter meta
        ev = next((e for e in await self._events(region, user_id) if e.eventId == eid), None)
        chapters = chapters_from_event(ev)

        async def fetch(cid: str) -> dict:
            return await self.bot.holo.get_event_leaderboard(  # type: ignore[union-attr]
                region, event_id=eid, chapter_id=cid, language=lang
            )

        title = f"{data.get('eventName', 'Event')} - Top 100"
        logo = self.bot.holo.unsquished_image_url(data.get("logo"))
        view = LeaderboardView(
            rows=rankings,
            title=title,
            thumb=logo,
            restrict_to=user_id,
            chapters=chapters,
            current_chapter=current_chapter,
            fetch=fetch,
            ends_at=data.get("chapterEndTime"),
        )
        return view, None

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
        await interaction.response.defer(thinking=True)
        view, err = await self._leaderboard_view(
            user_id=interaction.user.id, region=region, event=event
        )
        if err is not None:
            await interaction.followup.send(embed=err)
            return
        assert view is not None
        await view.send_initial(interaction)

    @event.command(name="player", description="Look up a player by name across every region's top 100.")
    @app_commands.describe(name="Player name (searches all regions' current top 100).")
    async def player(self, interaction: discord.Interaction, name: str) -> None:
        # slash twin of %player; the player search + card live in GraphCog, so delegate to it
        await interaction.response.defer(thinking=True)
        graph_cog = self.bot.get_cog("GraphCog")
        if graph_cog is None:
            await interaction.followup.send(
                embed=embeds.error_embed("Player lookup is unavailable right now.")
            )
            return
        view, err = await graph_cog._player_response(name, interaction.user.id)  # type: ignore[attr-defined]
        if err is not None:
            await interaction.followup.send(embed=err)
            return
        assert view is not None
        view.message = await interaction.followup.send(view=view, wait=True)

    @commands.command(name="leaderboard", aliases=["lb"])
    async def p_leaderboard(self, ctx: commands.Context, *args: str) -> None:
        # %leaderboard [region] - always the current event; region optional, nothing else
        region, tier, leftover = text_commands.parse_region_tier(list(args))
        if tier is not None or leftover:
            await ctx.reply(
                embed=text_commands.help_embed(
                    "leaderboard", "[region]", any_order=False, aliases=["lb"]
                ),
                mention_author=False,
            )
            return
        async with ctx.typing():
            view, err = await self._leaderboard_view(user_id=ctx.author.id, region=region or "default")
        if err is not None:
            await ctx.reply(embed=err, mention_author=False)
            return
        assert view is not None
        view.message = await ctx.reply(embed=view.render_embed(), view=view, mention_author=False)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(EventsCog(bot))
