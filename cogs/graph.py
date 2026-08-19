from __future__ import annotations

import asyncio
import io
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds, text_commands, timezones
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS, autocompletes
from helpers.chapter_buttons import ChapterButtons, chapters_from_event, fanmark_emoji
from helpers.views import HoloLayoutView, HoloView
from services.graph import render_graph as _render_graph_img
from services.graph import render_heatmap as _render_heatmap_img
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from datetime import tzinfo

    from data.models import EventInfo
    from main import HolodoriBot

_TIER = (90, 150, 255, 255)
_USER = (120, 200, 130, 255)
# player-card container accent per region (matches the graph line palette: us blue / as green / jp red)
_REGION_ACCENT = {"us": 0x5B93EC, "as": 0x4AD79A, "jp": 0xE76A6A}
_MODE_CHOICES = [
    app_commands.Choice(name="Total", value="total"),
    app_commands.Choice(name="Song Scores", value="song"),
]


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

    async def _resolve_event(self, region: str, event_id: str | None) -> EventInfo | None:
        assert self.bot.data and self.bot.holo
        try:
            events = await self.bot.data.events(region, self.bot.holo.lang)
        except HolodoriError:
            return None
        return next((e for e in events if e.eventId == event_id), events[0] if events else None)

    async def render_graph(
        self,
        *,
        region: str,
        tier: int,
        event_id: str | None,
        chapter: str | None,
        border: bool,
        music: bool,
        music_id: str | None,
        song_title: str | None,
        tz: tzinfo,
        ev: EventInfo | None,
        predict: bool = False,
        player_id: str | None = None,
        player_only: bool = False,
    ) -> tuple[discord.Embed, list[discord.File]]:
        assert self.bot.holo
        try:
            data = await self.bot.holo.get_event_graph(
                region,
                tier,
                event_id=event_id,
                chapter_id=chapter,
                border=border or None,
                music=music or None,
                music_id=music_id,
                user_id=player_id,
            )
        except HolodoriError as e:
            # 400s are our own validation (bad region/tier), whose detail is a ready-to-show
            # sentence like "This tier does not exist as a cutoff on this region."
            if e.status == 400 and e.detail:
                return embeds.error_embed(e.detail), []
            return embeds.error_embed(f"Couldn't fetch graph: {e.detail or e.status}"), []
        tier_series = data.get("tier") or []
        user_series = data.get("user") or []
        label = "Song Scores" if music else "Cutoff"
        lines: list[tuple[str, list, tuple[int, int, int, int]]] = []
        # player-only (a tracked history player): just their line, no tier cutoff to compare against
        if tier_series and not player_only:
            lines.append((f"Tier {tier} {label}", tier_series, _TIER))
        if user_series:
            lines.append((str(data.get("name") or f"#{tier}"), user_series, _USER))
        if not lines:
            return embeds.error_embed(
                "No data for that player this chapter yet." if player_only
                else "No graph data for that event/tier yet."
            ), []
        prediction = data.get("prediction")
        predict_note: str | None = None
        if predict and not prediction:
            # graceful for the PREDICT toggle: render the graph (no projection line) + a note,
            # instead of replacing the whole graph with an error embed
            predict_note = (
                "Predictions are only for relay (event-point) events, not song-score / per-song boards."
                if music or data.get("songScore")
                else "Not enough data yet to project a final (needs ~5% of the event)."
            )
        suffix = f" - {song_title}" if music and song_title else ""
        title = (
            f"{data.get('name') or 'Player'} - {REGION_LABELS.get(region, region)}"
            if player_only
            else f"Tier {tier} {label} - {REGION_LABELS.get(region, region)}{suffix}"
        )
        img = await asyncio.to_thread(
            _render_graph_img, lines, title, tz=tz, prediction=prediction if predict else None
        )

        embed = embeds.embed(title=title)
        parts: list[str] = []
        last_ms = max((int(p[0]) for _, s, _ in lines for p in s), default=0)
        if last_ms:
            parts.append(f"**Last Data Update:** <t:{last_ms // 1000}:R>")
        if predict and prediction:
            pm = prediction.get("plusMinus")
            pm_str = f" ± {int(pm):,}" if pm else ""
            parts.append(
                f"**Predicted T{tier} final:** {prediction['final']:,}{pm_str} EP "
                f"(<t:{prediction['endTime'] // 1000}:R>)"
            )
        elif predict_note:
            parts.append(f"*{predict_note}*")
        if parts:
            embed.description = "\n".join(parts)
        files = [discord.File(io.BytesIO(img), "graph.png")]
        logo = self.bot.holo.unsquished_image_url(ev.logo) if ev and ev.logo else None
        if logo:
            embed.set_thumbnail(url=logo)
        embed.set_image(url="attachment://graph.png")
        return embed, files

    async def _graph_payload(
        self,
        *,
        user_id: int,
        region: str,
        tier: int,
        event: str | None = None,
        mode: str = "total",
        border: bool = False,
        predict: bool = False,
        timezone: str | None = None,
        player_id: str | None = None,
        player_only: bool = False,
        chapter: str | None = None,
    ) -> tuple[discord.Embed, list[discord.File], _GraphView | None]:
        # the shared body of `/graph cutoff` and `%graph`: resolve, render, and build the interactive
        # view (or None when there's nothing to interact with). the caller sends it. player_id (with
        # player_only) follows one specific player by id across chapters instead of a tier's cutoff.
        # an explicit `chapter` pins the view to it (used by %player's chapter switch); else it defaults.
        assert self.bot.holo and self.bot.user_data and self.bot.data
        region = await self._region(user_id, region)
        tz_name = timezone or await self.bot.user_data.get_settings(user_id, "timezone")
        tz = timezones.resolve(tz_name)
        ev = await self._resolve_event(region, event)
        # default to the chapter live right now (else the latest)
        if chapter is None:
            chapter = (ev.activeChapterId or (ev.chapters[-1] if ev.chapters else None)) if ev else None
        music = mode == "song"

        songs: list[tuple[str, str]] = []  # (music_id, title)
        music_id: str | None = None
        song_title: str | None = None
        if music:
            try:
                lb = await self.bot.holo.get_event_leaderboard(
                    region, event_id=event, chapter_id=chapter, music=True
                )
                songs = [(m["musicId"], m.get("title") or m["musicId"]) for m in (lb.get("musics") or [])]
            except HolodoriError:
                songs = []
            if songs:
                music_id, song_title = songs[0][0], songs[0][1]

        embed, files = await self.render_graph(
            region=region,
            tier=tier,
            event_id=event,
            chapter=chapter,
            border=border,
            music=music,
            music_id=music_id,
            song_title=song_title,
            tz=tz,
            ev=ev,
            predict=predict,
            player_id=player_id,
            player_only=player_only,
        )
        # a view is worth showing when there's more than one chapter to switch between, a song
        # dropdown to offer, or a prediction to toggle (relay/EP events in total mode)
        chapters = chapters_from_event(ev)
        can_predict = (not music) and ev is not None and not ev.isSongScore
        if not files or (len(chapters) <= 1 and not songs and not can_predict):
            return embed, files, None
        view = _GraphView(
            self,
            region=region,
            tier=tier,
            event_id=event,
            chapter=chapter,
            border=border,
            tz=tz,
            ev=ev,
            music=music,
            songs=songs,
            music_id=music_id,
            predict=predict,
            can_predict=can_predict,
            player_id=player_id,
            player_only=player_only,
            restrict_to=user_id,
        )
        return embed, files, view

    @graph.command(name="cutoff", description="Graph a tier's cutoff over the event.")
    @app_commands.describe(
        tier="Tier rank to graph (e.g. 100).",
        region="Game server region.",
        event="Event (defaults to the latest).",
        mode="Total event score or per-song scores.",
        border="Use the tier border line.",
        predict="Project the final cutoff (relay events only).",
        timezone="Timezone for the time axis (defaults to your setting).",
    )
    @app_commands.choices(region=REGION_CHOICES, mode=_MODE_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event(), tier=autocompletes.tier())
    async def cutoff(
        self,
        interaction: discord.Interaction,
        tier: int,
        region: str = "default",
        event: str | None = None,
        mode: str = "total",
        border: bool = False,
        predict: bool = False,
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
        embed, files, view = await self._graph_payload(
            user_id=interaction.user.id,
            region=region,
            tier=tier,
            event=event,
            mode=mode,
            border=border,
            predict=predict,
            timezone=timezone,
        )
        if view is None:
            await interaction.followup.send(embed=embed, files=files)
            return
        view.message = await interaction.followup.send(embed=embed, files=files, view=view, wait=True)

    async def _heatmap_payload(
        self,
        *,
        user_id: int,
        region: str,
        tier: int,
        event: str | None = None,
        by_tier: bool | None = None,
        timezone: str | None = None,
        player_id: str | None = None,
        metric: str = "gph",
        chapter: str | None = None,
    ) -> tuple[discord.Embed, list[discord.File], _HeatmapView | None]:
        # the shared body of `/heatmap` and `%heatmap` (metric toggle + chapter-switch view). the caller
        # sends. player_id follows one specific player by id (e.g. a dropped-off one) across chapters.
        # an explicit `chapter` pins it (used by %player's chapter switch); else it defaults.
        assert self.bot.holo and self.bot.user_data and self.bot.data
        region = await self._region(user_id, region)
        tz_name = timezone or await self.bot.user_data.get_settings(user_id, "timezone")
        tz = timezones.resolve(tz_name)
        ev = await self._resolve_event(region, event)
        # default to the chapter live right now (else the latest)
        if chapter is None:
            chapter = (ev.activeChapterId or (ev.chapters[-1] if ev.chapters else None)) if ev else None
        player_mode = (tier <= 100) if by_tier is None else (not by_tier)

        embed, files = await self.render_heatmap(
            region=region,
            tier=tier,
            event_id=event,
            chapter=chapter,
            player_mode=player_mode,
            tz=tz,
            ev=ev,
            player_id=player_id,
            metric=metric,
        )
        # a view is always attached so the GPH<->EPH toggle is available; chapter buttons only show
        # on multi-chapter (relay) events. skipped only when there's no image to toggle.
        if not files:
            return embed, files, None
        view = _HeatmapView(
            self,
            region=region,
            tier=tier,
            event_id=event,
            chapter=chapter,
            player_mode=player_mode,
            tz=tz,
            ev=ev,
            player_id=player_id,
            metric=metric,
            restrict_to=user_id,
        )
        return embed, files, view

    @app_commands.command(
        name="heatmap",
        description="Hourly gains-per-hour (GPH) heatmap for a player, or a tier's cutoff. Toggle EPH.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        tier="Rank to track. Ranks 1-100 track that player; a border tracks the cutoff.",
        by_tier="Track the cutoff line itself instead of the player at that rank.",
        region="Game server region.",
        event="Event (defaults to the latest).",
        timezone="Timezone for the hour columns (defaults to your setting).",
    )
    @app_commands.choices(region=REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event(), tier=autocompletes.tier())
    async def heatmap(
        self,
        interaction: discord.Interaction,
        tier: int,
        by_tier: bool | None = None,
        region: str = "default",
        event: str | None = None,
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
        # a border (rank > 100) has no single player behind it, so it can only be a cutoff heatmap
        if by_tier is False and tier > 100:
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"Rank {tier} has no single player behind it (only ranks 1-100 do). "
                    "Set `by_tier: true` for a cutoff heatmap."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        embed, files, view = await self._heatmap_payload(
            user_id=interaction.user.id,
            region=region,
            tier=tier,
            event=event,
            by_tier=by_tier,
            timezone=timezone,
        )
        if view is None:
            await interaction.followup.send(embed=embed, files=files)
            return
        view.message = await interaction.followup.send(embed=embed, files=files, view=view, wait=True)

    async def render_heatmap(
        self,
        *,
        region: str,
        tier: int,
        event_id: str | None,
        chapter: str | None,
        player_mode: bool,
        tz: tzinfo,
        ev: EventInfo | None,
        player_id: str | None = None,
        metric: str = "gph",
    ) -> tuple[discord.Embed, list[discord.File]]:
        assert self.bot.holo
        try:
            # player mode needs both the player series and the cutoff series (the latter is our
            # fetch coverage); a cutoff heatmap needs only the cutoff, so border=True skips the lookup.
            # player_id follows a specific (e.g. dropped-off) player by id instead of by rank.
            data = await self.bot.holo.get_event_graph(
                region, tier, event_id=event_id, chapter_id=chapter,
                border=not player_mode, user_id=player_id,
            )
        except HolodoriError as e:
            msg = e.detail if e.status == 400 and e.detail else f"Couldn't fetch graph: {e.detail or e.status}"
            return embeds.error_embed(msg), []
        tier_series = data.get("tier") or []
        if len(tier_series) < 2:
            return embeds.error_embed("Not enough cutoff data for that event/tier yet."), []
        coverage = [int(p[0]) for p in tier_series]
        # the axis spans THIS chapter (backend now returns chapter-scoped start/end), not the event
        start = data.get("startTime") or coverage[0]
        end = data.get("endTime") or coverage[-1]

        label = "Gains Per Hour" if metric == "gph" else "Event Points Per Hour"
        presence: list[int] | None = None
        if player_mode:
            user_series = data.get("user") or []
            if len(user_series) < 2:
                return embeds.error_embed(f"No tracked player is at T{tier} for this event."), []
            value_series = user_series
            presence = [int(p[0]) for p in user_series]
            title = f"{data.get('name') or f'T{tier}'} {label} - {REGION_LABELS.get(region, region)}"
        else:
            value_series = tier_series
            title = f"Tier {tier} Cutoff {label} - {REGION_LABELS.get(region, region)}"

        img = await asyncio.to_thread(
            _render_heatmap_img,
            value_series,
            title,
            start_ms=start,
            end_ms=end,
            now_ms=int(time.time() * 1000),
            coverage=coverage,
            presence=presence,
            tz=tz,
            mode=metric,
        )
        embed = embeds.embed(title=title)
        last_ms = max((int(p[0]) for p in value_series), default=0)
        if last_ms:
            embed.description = f"**Last Data Update:** <t:{last_ms // 1000}:R>"
        files = [discord.File(io.BytesIO(img), "heatmap.png")]
        logo = self.bot.holo.unsquished_image_url(ev.logo) if ev and ev.logo else None
        if logo:
            embed.set_thumbnail(url=logo)
        embed.set_image(url="attachment://heatmap.png")
        return embed, files

    @app_commands.command(
        name="cutoff",
        description="Cutoff points, speed, and the projected final for a tier (text).",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        tier="Tier rank (e.g. 100).",
        region="Game server region.",
        event="Event (defaults to the latest).",
    )
    @app_commands.choices(region=REGION_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event(), tier=autocompletes.tier())
    async def cutoff_text(
        self,
        interaction: discord.Interaction,
        tier: int,
        region: str = "default",
        event: str | None = None,
    ) -> None:
        assert self.bot.holo and self.bot.user_data and self.bot.data
        await interaction.response.defer(thinking=True)
        em = await self._cutoff_text_embed(
            user_id=interaction.user.id, region=region, tier=tier, event=event
        )
        await interaction.followup.send(embed=em)

    async def _cutoff_text_embed(
        self,
        *,
        user_id: int,
        region: str,
        tier: int,
        event: str | None = None,
        chapter: str | None = None,
    ) -> discord.Embed:
        # the shared body of `/cutoff` and `%cutoff`: a text stats + prediction card (no view).
        # returns an error embed (same wording as the slash command) on a bad tier / no data. an
        # explicit `chapter` pins it (used by a %player card on a past chapter); else it defaults.
        assert self.bot.holo and self.bot.user_data
        region = await self._region(user_id, region)
        ev = await self._resolve_event(region, event)
        # default to the chapter live right now (else the latest)
        if chapter is None:
            chapter = (ev.activeChapterId or (ev.chapters[-1] if ev.chapters else None)) if ev else None
        try:
            data = await self.bot.holo.get_event_graph(
                region, tier, event_id=event, chapter_id=chapter
            )
        except HolodoriError as e:
            msg = e.detail if e.status == 400 and e.detail else f"Couldn't fetch cutoff: {e.detail or e.status}"
            return embeds.error_embed(msg)
        series = [(int(p[0]), float(p[1])) for p in (data.get("tier") or [])]
        if len(series) < 2:
            return embeds.error_embed("Not enough cutoff data for that event/tier yet.")

        last_ms, current = series[-1]
        start = data.get("startTime") or series[0][0]
        end = data.get("endTime") or last_ms
        elapsed_h = max((last_ms - start) / 3_600_000, 1 / 60)
        avg_hr = current / elapsed_h
        # gain over the last hour of data
        target = last_ms - 3_600_000
        prev = next((v for t, v in reversed(series) if t <= target), series[0][1])
        last_hr = current - prev
        # biggest gain over any rolling 1-hour window (two-pointer over the sorted series)
        peak_hr = last_hr
        lead = 0
        for t, v in series:
            while lead + 1 < len(series) and series[lead + 1][0] <= t - 3_600_000:
                lead += 1
            if series[lead][0] <= t - 3_600_000:
                peak_hr = max(peak_hr, v - series[lead][1])
        pct = (last_ms - start) / max(1, end - start) * 100
        unit = "" if data.get("songScore") else " EP"

        chap = ev.chapterMeta.get(chapter) if ev and chapter else None
        who = f" ({chap.shortName})" if chap and chap.shortName else ""
        title = f"{ev.name if ev else 'Event'}{who} - T{tier} Cutoff"

        em = embeds.embed(
            title=title, description=f"**Requested:** <t:{int(time.time())}:R>"
        )
        em.add_field(
            name="Cutoff Statistics",
            value=(
                f"**Points:** {int(current):,}{unit}\n"
                f"**Average:** {int(avg_hr):,}/hr\n"
                f"**Last hour:** {int(last_hr):,}/hr\n"
                f"**Peak gain:** {int(peak_hr):,}/hr"
            ),
            inline=False,
        )
        em.add_field(
            name="Event Information",
            value=(
                f"**Started:** <t:{start // 1000}:f>\n"
                f"**Ends:** <t:{end // 1000}:f> (<t:{end // 1000}:R>)\n"
                f"**Progress:** {pct:.2f}%\n"
                f"**Data as of:** <t:{last_ms // 1000}:R>"
            ),
            inline=False,
        )
        pred = data.get("prediction")
        if pred:
            pm = pred.get("plusMinus")
            est_value = f"**Estimated final:** `{int(pred['final']):,}{unit}`"
            if pm:
                est_value += f" (± `{int(pm):,}`)"
            lo, hi = pred.get("finalLow"), pred.get("finalHigh")
            if lo is not None and hi is not None:
                est_value += f"\n**Range:** `{int(lo):,}` to `{int(hi):,}`{unit}"
        elif data.get("songScore"):
            est_value = "Not available for song-score boards."
        else:
            est_value = "Too early to project a final yet."
        em.add_field(name="Point Estimation (Prediction)", value=est_value, inline=False)

        logo = self.bot.holo.unsquished_image_url(ev.logo) if ev and ev.logo else None
        if logo:
            em.set_thumbnail(url=logo)
        return em

    # --- %-prefix text commands: mirror the slash commands, region + tier in any order ---

    @commands.command(name="cutoff")
    async def p_cutoff(self, ctx: commands.Context, *args: str) -> None:
        region, tier, leftover = text_commands.parse_region_tier(list(args))
        if tier is None or leftover:
            await ctx.reply(
                embed=text_commands.help_embed("cutoff", "[region] {tier}", any_order=True, aliases=[]),
                mention_author=False,
            )
            return
        async with ctx.typing():
            em = await self._cutoff_text_embed(user_id=ctx.author.id, region=region or "default", tier=tier)
        await ctx.reply(embed=em, mention_author=False)

    @commands.command(name="graph", aliases=["eph"])
    async def p_graph(self, ctx: commands.Context, *args: str) -> None:
        region, tier, leftover = text_commands.parse_region_tier(list(args))
        if tier is None or leftover:
            await ctx.reply(
                embed=text_commands.help_embed("graph", "[region] {tier}", any_order=True, aliases=["eph"]),
                mention_author=False,
            )
            return
        async with ctx.typing():
            embed, files, view = await self._graph_payload(
                user_id=ctx.author.id, region=region or "default", tier=tier
            )
        msg = await ctx.reply(embed=embed, files=files, view=view, mention_author=False)
        if view is not None:
            view.message = msg

    @commands.command(name="heatmap")
    async def p_heatmap(self, ctx: commands.Context, *args: str) -> None:
        region, tier, leftover = text_commands.parse_region_tier(list(args))
        if tier is None or leftover:
            await ctx.reply(
                embed=text_commands.help_embed("heatmap", "[region] {tier}", any_order=True, aliases=[]),
                mention_author=False,
            )
            return
        async with ctx.typing():
            embed, files, view = await self._heatmap_payload(
                user_id=ctx.author.id, region=region or "default", tier=tier
            )
        msg = await ctx.reply(embed=embed, files=files, view=view, mention_author=False)
        if view is not None:
            view.message = msg

    async def _build_player_card(
        self,
        result: dict,
        *,
        query: str,
        ev: "EventInfo | None",
        chapter: str | None,
        restrict_to: int,
    ) -> "_PlayerCardView":
        # a components-v2 card built from ONE chapter-scoped search result: the {Name} - {region}
        # heading, the Points stat + gains, and the Graph / Heatmap / Cutoff buttons all live INSIDE
        # one accent Container. the result already carries this chapter's standing (live rank + points
        # for the current chapter, else best-rank/history); a small fetch adds gains (and fills in the
        # points/updatedAt for a past chapter, which the index leaves out).
        region = result["region"]
        uid = str(result["userId"]) if result.get("userId") else None
        gain_stats: dict | None = None
        if uid and self.bot.holo:
            try:
                gain_stats = await self.bot.holo.get_player_stats(region, uid, chapter_id=chapter)
            except HolodoriError:
                gain_stats = None
        s = gain_stats or {}
        rank = result.get("rank")
        points = result.get("points") if result.get("points") is not None else s.get("points")
        return _PlayerCardView(
            self,
            region=region,
            rank=int(rank) if rank is not None else None,
            name=str(result["name"]),
            points=points,
            updated=result.get("updatedAt") or s.get("updatedAt"),
            public_id=uid,
            history=bool(result.get("history")),
            ended=bool(result.get("ended")),
            gain_stats=gain_stats,
            event_id=result.get("eventId"),
            ev=ev,
            chapter=chapter,
            query=query,
            restrict_to=restrict_to,
        )

    async def _player_research(
        self, query: str, *, chapter: str | None, ev: "EventInfo | None", restrict_to: int
    ) -> "HoloLayoutView":
        # RE-SEARCH the query in one chapter across all regions and return the right view for it: a
        # card (one clear match), a picker (several close matches), or a no-match view - each carrying
        # chapter buttons that re-run this in another chapter. driven by the %player chapter buttons.
        results: list[dict] = []
        if self.bot.holo:
            try:
                results = await self.bot.holo.search_players(query, chapter=chapter)
            except HolodoriError:
                results = []
        if not results:
            return _PlayerNoMatchView(
                self, query=query, ev=ev, chapter=chapter, restrict_to=restrict_to
            )
        # identical / very-close matches (e.g. the same name on two regions) -> ask which one
        top = results[0]
        close = [r for r in results if top["match"] - r["match"] <= 6][:10]
        if len(close) >= 2:
            return _PlayerPickView(
                self, close, query=query, ev=ev, chapter=chapter, restrict_to=restrict_to
            )
        return await self._build_player_card(
            top, query=query, ev=ev, chapter=chapter, restrict_to=restrict_to
        )

    async def _player_response(
        self, query: str, requester_id: int
    ) -> tuple["HoloLayoutView | None", "discord.Embed | None"]:
        # shared by %player and /event player: (v2 view, None) to send a card/picker/no-match (all with
        # chapter buttons), or (None, embed) only for the empty-query usage message. the search runs in
        # the current chapter first; its chapter buttons re-search any other chapter.
        query = query.strip()
        if not query:
            return None, text_commands.help_embed("player", "{name}", any_order=False, aliases=[])
        assert self.bot.holo
        region = await self._region(requester_id, "default")
        ev = await self._resolve_event(region, None)
        chapter = (ev.activeChapterId or (ev.chapters[-1] if ev.chapters else None)) if ev else None
        view = await self._player_research(
            query, chapter=chapter, ev=ev, restrict_to=requester_id
        )
        return view, None

    @commands.command(name="player")
    async def p_player(self, ctx: commands.Context, *, query: str = "") -> None:
        # %player {name} - greedily takes the whole rest as the name; searches every region's t100
        async with ctx.typing():
            view, err = await self._player_response(query, ctx.author.id)
        if err is not None:
            await ctx.reply(embed=err, mention_author=False)
            return
        assert view is not None
        view.message = await ctx.reply(view=view, mention_author=False)


class _GraphView(HoloView):
    """holomem chapter buttons (relay events) plus, in Song-Scores mode, a song dropdown; re-renders
    the cutoff graph on either."""

    def __init__(
        self,
        cog: GraphCog,
        *,
        region: str,
        tier: int,
        event_id: str | None,
        chapter: str | None,
        border: bool,
        tz: tzinfo,
        ev: EventInfo | None,
        music: bool,
        songs: list[tuple[str, str]],
        music_id: str | None,
        predict: bool,
        can_predict: bool = False,
        player_id: str | None = None,
        player_only: bool = False,
        restrict_to: int,
    ) -> None:
        super().__init__(timeout=180, restrict_to=restrict_to)
        self.cog = cog
        self.region = region
        self.tier = tier
        self.event_id = event_id
        self.chapter = chapter
        self.border = border
        self.tz = tz
        self.ev = ev
        self.music = music
        self.predict = predict
        self.music_id = music_id
        self.player_id = player_id
        self.player_only = player_only
        self._song_list = songs
        self._songs = dict(songs)
        self._chapters = ChapterButtons(chapters_from_event(ev), chapter, self._on_chapter)
        self._chapters.add_to(self)
        # the song dropdown (song mode) and the PREDICT toggle (total mode) are mutually exclusive,
        # so both sit on the row just below the chapter buttons
        self._select: discord.ui.Select | None = None
        self._predict_btn: discord.ui.Button | None = None
        if music and songs:
            self._select = discord.ui.Select(
                placeholder="Choose a song...", row=self._chapters.rows_used
            )
            self._select.callback = self._on_song  # type: ignore[method-assign]
            self.add_item(self._select)
            self._sync_options()
        elif can_predict:
            self._predict_btn = discord.ui.Button(
                label="PREDICT", emoji="🔮", row=self._chapters.rows_used
            )
            self._style_predict()
            self._predict_btn.callback = self._on_predict  # type: ignore[method-assign]
            self.add_item(self._predict_btn)

    def _style_predict(self) -> None:
        if self._predict_btn is not None:
            self._predict_btn.style = (
                discord.ButtonStyle.primary if self.predict else discord.ButtonStyle.secondary
            )

    async def _on_predict(self, interaction: discord.Interaction) -> None:
        self.predict = not self.predict
        self._style_predict()
        await interaction.response.defer()
        await self._rerender(interaction)

    def _sync_options(self) -> None:
        # rebuild the options every time so the picked song stays selected in the dropdown. without
        # this the dropdown keeps its original default, so re-picking that song registers as no
        # change and discord ignores it
        if self._select is None:
            return
        self._select.options = [
            discord.SelectOption(label=title[:100], value=mid, default=mid == self.music_id)
            for mid, title in self._song_list[:25]
        ] or [discord.SelectOption(label="(no songs)", value="_none")]

    async def _rerender(self, interaction: discord.Interaction) -> None:
        embed, files = await self.cog.render_graph(
            region=self.region,
            tier=self.tier,
            event_id=self.event_id,
            chapter=self.chapter,
            border=self.border,
            music=self.music,
            music_id=self.music_id,
            song_title=self._songs.get(self.music_id) if self.music_id else None,
            tz=self.tz,
            ev=self.ev,
            predict=self.predict,
            player_id=self.player_id,
            player_only=self.player_only,
        )
        await interaction.edit_original_response(embed=embed, attachments=files, view=self)

    async def _on_song(self, interaction: discord.Interaction) -> None:
        self.music_id = interaction.data["values"][0]  # type: ignore[index]
        self._sync_options()
        await interaction.response.defer()
        await self._rerender(interaction)

    async def _on_chapter(self, interaction: discord.Interaction, chapter_id: str) -> None:
        await interaction.response.defer()
        self.chapter = chapter_id
        self._chapters.set_current(chapter_id)
        if self.music:
            # each chapter has its own song boards; refetch and reset to the first
            try:
                lb = await self.cog.bot.holo.get_event_leaderboard(  # type: ignore[union-attr]
                    self.region, event_id=self.event_id, chapter_id=chapter_id, music=True
                )
                self._song_list = [
                    (m["musicId"], m.get("title") or m["musicId"]) for m in (lb.get("musics") or [])
                ]
            except HolodoriError:
                self._song_list = []
            self._songs = dict(self._song_list)
            self.music_id = self._song_list[0][0] if self._song_list else None
            self._sync_options()
        await self._rerender(interaction)


class _HeatmapView(HoloView):
    """A GPH<->EPH metric toggle plus, on relay events, holomem chapter buttons - each re-renders."""

    def __init__(
        self,
        cog: GraphCog,
        *,
        region: str,
        tier: int,
        event_id: str | None,
        chapter: str | None,
        player_mode: bool,
        tz: tzinfo,
        ev: EventInfo | None,
        player_id: str | None = None,
        metric: str = "gph",
        restrict_to: int,
    ) -> None:
        super().__init__(timeout=180, restrict_to=restrict_to)
        self.cog = cog
        self.region = region
        self.tier = tier
        self.event_id = event_id
        self.chapter = chapter
        self.player_mode = player_mode
        self.tz = tz
        self.ev = ev
        self.player_id = player_id
        self.metric = metric
        # chapter buttons first (from row 0); the toggle sits on the row just below them (or row 0
        # when there are no chapter buttons). label names the metric a click switches TO
        self._chapters = ChapterButtons(
            chapters_from_event(ev), chapter, self._on_chapter, max_rows=4
        )
        self._chapters.add_to(self)
        self._toggle = discord.ui.Button(
            label="Show EPH" if metric == "gph" else "Show GPH",
            emoji="🔁",
            style=discord.ButtonStyle.secondary,
            row=self._chapters.rows_used,
        )
        self._toggle.callback = self._on_toggle  # type: ignore[method-assign]
        self.add_item(self._toggle)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        embed, files = await self.cog.render_heatmap(
            region=self.region,
            tier=self.tier,
            event_id=self.event_id,
            chapter=self.chapter,
            player_mode=self.player_mode,
            tz=self.tz,
            ev=self.ev,
            player_id=self.player_id,
            metric=self.metric,
        )
        # keep the view even if this chapter has no data yet (so they can switch back / toggle)
        await interaction.edit_original_response(embed=embed, attachments=files, view=self)

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.metric = "eph" if self.metric == "gph" else "gph"
        self._toggle.label = "Show EPH" if self.metric == "gph" else "Show GPH"
        await self._rerender(interaction)

    async def _on_chapter(self, interaction: discord.Interaction, chapter_id: str) -> None:
        await interaction.response.defer()
        self.chapter = chapter_id
        self._chapters.set_current(chapter_id)
        await self._rerender(interaction)


def _format_gains(g: dict) -> str | None:
    """The "Gains" section body: gain amounts + the estimated gain method (overall and last hour).
    Returns None when the player has no recorded gains, so the whole section is skipped."""
    last = g.get("lastGain")
    if last is None:
        return None
    lines = ["## Gains"]
    total = g.get("gainCount")
    if total is not None:
        lines.append(f"**Total Gains:** {int(total):,}")  # every gain this chapter, spacing aside
    line = f"**Last Gain Amount:** +{int(last):,} EP"
    if g.get("lastGainAt"):
        line += f" (<t:{int(g['lastGainAt']) // 1000}:R>)"
    lines.append(line)
    avg = g.get("avgGain")
    if avg is not None:
        lines.append(f"**Average Gain Amount (Last 100):** +{int(avg):,} EP")
    # a null method from the backend = too few gains to call it; "No Gain" is an explicit last-hour case
    lines.append(f"**Estimated Most Used Method:** {g.get('method') or 'Not enough info'}")
    lines.append(
        f"**Estimated Most Used Method (Last Hour):** {g.get('methodHour') or 'Not enough info'}"
    )
    return "\n".join(lines)


def _chapter_label(ev: "EventInfo | None", cid: str | None) -> str | None:
    for c, label, *_ in chapters_from_event(ev):
        if c == cid:
            return label
    return None


class _ResearchView(HoloLayoutView):
    """Base for the three %player result views (card, picker, no-match). Carries the query + the chapter
    being shown so its chapter buttons - only on a multi-chapter event, UNDER the card - RE-RUN the
    search in the picked chapter, yielding a fresh card / picker / no-match each time."""

    def __init__(
        self,
        cog: GraphCog,
        *,
        query: str,
        ev: "EventInfo | None",
        chapter: str | None,
        restrict_to: int,
        timeout: float = 180,
    ) -> None:
        super().__init__(timeout=timeout, restrict_to=restrict_to)
        self.cog = cog
        self.query = query
        self.ev = ev
        self.chapter = chapter

    def _add_chapter_rows(self) -> None:
        chapters = chapters_from_event(self.ev)
        if len(chapters) <= 1:
            return
        row = discord.ui.ActionRow()
        for i, (cid, clabel, started, char_id) in enumerate(chapters[:20]):
            if i and i % 5 == 0:  # 5 buttons per row
                self.add_item(row)
                row = discord.ui.ActionRow()
            btn = discord.ui.Button(label=(clabel or "?")[:80], emoji=fanmark_emoji(char_id))
            if cid == self.chapter:
                btn.style, btn.disabled = discord.ButtonStyle.success, True  # the one shown now
            elif not started:
                btn.style, btn.disabled = discord.ButtonStyle.secondary, True  # not started yet
            else:
                btn.style = discord.ButtonStyle.primary  # re-search this one

            async def cb(interaction: discord.Interaction, cid: str = cid) -> None:
                await self._on_chapter(interaction, cid)

            btn.callback = cb  # type: ignore[method-assign]
            row.add_item(btn)
        self.add_item(row)

    async def _on_chapter(self, interaction: discord.Interaction, chapter_id: str) -> None:
        if chapter_id == self.chapter:
            await interaction.response.defer()
            return
        await interaction.response.defer()  # deferred UPDATE; the re-search edits this same message
        view = await self.cog._player_research(
            self.query, chapter=chapter_id, ev=self.ev, restrict_to=self.restrict_to or 0
        )
        await interaction.edit_original_response(view=view)
        view.message = interaction.message
        self.stop()  # the new view owns the message now; don't let this one's timeout clobber it


class _PlayerCardView(_ResearchView):
    """components-v2 %player card: the {Name} - {region} heading, the Points stat, and the Graph /
    Heatmap / Cutoff buttons all sit INSIDE one accent Container (buttons inside the "embed"). each
    button posts its own view as a follow-up so the card itself stays put. chapter buttons underneath
    re-search the query in another chapter."""

    def __init__(
        self,
        cog: GraphCog,
        *,
        region: str,
        rank: int | None,
        name: str,
        points: int | None,
        updated: int | None = None,
        public_id: str | None = None,
        history: bool = False,
        ended: bool = False,
        gain_stats: dict | None = None,
        event_id: str | None = None,
        ev: "EventInfo | None" = None,
        chapter: str | None = None,
        query: str = "",
        restrict_to: int,
    ) -> None:
        super().__init__(cog, query=query, ev=ev, chapter=chapter, restrict_to=restrict_to)
        self.region = region
        self.rank = rank
        self.public_id = public_id
        self.history = history
        self.event_id = event_id
        has_data = rank is not None  # no top-100 standing this chapter -> nothing to graph/heatmap
        label = REGION_LABELS.get(region, region)
        pts = f"{int(points):,} EP" if points is not None else "—"
        container = discord.ui.Container(
            accent_colour=discord.Colour(_REGION_ACCENT.get(region, 0x8B5CF6))
        )
        container.add_item(
            discord.ui.TextDisplay(f"## {discord.utils.escape_markdown(name)} - {label}")
        )
        # rank meaning: a dropped-off player left the board before the chapter ended, so `rank` is the
        # best they reached; otherwise it's their standing on the reference board - their live rank on
        # the current chapter, or their FINAL rank on a finished one.
        if rank is None:
            rank_line = "Not in the top 100 this chapter"
        elif history:
            rank_line = f"No longer in T100, reached T{rank}"
        elif ended:
            rank_line = f"Final rank {rank}"
        else:
            rank_line = f"Currently rank {rank}"
        stats = f"## Player Statistics\n**Points:** {pts}\n{rank_line}"
        if updated:
            stats += f"\n**Last Data Update:** <t:{int(updated) // 1000}:R>"
        container.add_item(discord.ui.TextDisplay(stats))
        # a separate "Gains" section below the stats: gain amounts + the estimated gain method (every
        # gain is captured, since minute-by-minute tracking has no gaps)
        gains_text = _format_gains(gain_stats) if gain_stats else None
        if gains_text:
            container.add_item(discord.ui.TextDisplay(gains_text))
        # a current player is graphed by their live rank; a dropped-off one has no live rank, so
        # graph/heatmap follow them by their id instead (needs public_id). Cutoff is a tier's line,
        # not a player's, so it's current-players-only.
        specs: list[tuple[str, str, object]] = []
        if has_data and (not history or public_id):
            specs.append(("Graph", "📈", self._on_graph))
            specs.append(("Heatmap", "🔥", self._on_heatmap))
        if has_data and not history:
            specs.append(("Cutoff", "✂️", self._on_cutoff))
        if public_id:  # the profile fetch keys on the public id, only known from the search
            specs.append(("Profile", "👤", self._on_profile))
        if specs:
            row = discord.ui.ActionRow()
            for lbl, emo, cb in specs:
                btn = discord.ui.Button(label=lbl, emoji=emo, style=discord.ButtonStyle.primary)
                btn.callback = cb  # type: ignore[assignment]
                row.add_item(btn)
            container.add_item(row)
        self.add_item(container)
        self._add_chapter_rows()  # chapter buttons UNDER the card (multi-chapter events): re-search

    async def _send(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        files: list[discord.File],
        view: HoloView | None,
    ) -> None:
        if view is None:
            await interaction.followup.send(embed=embed, files=files)
            return
        view.message = await interaction.followup.send(
            embed=embed, files=files, view=view, wait=True
        )

    async def _on_graph(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        # a history player is followed by id (player-only, no cutoff line); a current one by rank.
        # pin to the chapter the card is showing (the buttons may have switched it off the live one)
        embed, files, view = await self.cog._graph_payload(
            user_id=interaction.user.id,
            region=self.region,
            tier=self.rank or 0,
            event=self.event_id,
            chapter=self.chapter,
            player_id=self.public_id if self.history else None,
            player_only=self.history,
        )
        await self._send(interaction, embed, files, view)

    async def _on_heatmap(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        embed, files, view = await self.cog._heatmap_payload(
            user_id=interaction.user.id,
            region=self.region,
            tier=self.rank or 0,
            event=self.event_id,
            chapter=self.chapter,
            player_id=self.public_id if self.history else None,
        )
        await self._send(interaction, embed, files, view)

    async def _on_cutoff(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        em = await self.cog._cutoff_text_embed(
            user_id=interaction.user.id,
            region=self.region,
            tier=self.rank or 0,
            event=self.event_id,
            chapter=self.chapter,
        )
        await interaction.followup.send(embed=em)

    async def _on_profile(self, interaction: discord.Interaction) -> None:
        # the profile card lives in ProfileCog; delegate to it with this player's public id
        await interaction.response.defer(thinking=True)
        prof = self.cog.bot.get_cog("ProfileCog")
        if prof is None:
            await interaction.followup.send(embed=embeds.error_embed("Couldn't load that profile."))
            return
        assert self.public_id is not None  # the Profile button is only added when we have one
        view, files, err = await prof.build_profile(self.public_id, self.region)  # type: ignore[attr-defined]
        if err is not None or view is None:
            await interaction.followup.send(embed=err or embeds.error_embed("Couldn't load that profile."))
            return
        await interaction.followup.send(view=view, files=files)


class _PlayerPickView(_ResearchView):
    """components-v2 disambiguation dropdown when a %player query matches more than one player in the
    chapter (e.g. the same name on two regions); picking one edits the message into that player's card
    (v2 -> v2). chapter buttons underneath re-search the query in another chapter."""

    def __init__(
        self,
        cog: GraphCog,
        candidates: list[dict],
        *,
        query: str,
        ev: "EventInfo | None",
        chapter: str | None,
        restrict_to: int,
    ) -> None:
        super().__init__(cog, query=query, ev=ev, chapter=chapter, restrict_to=restrict_to, timeout=120)
        self.candidates = candidates
        container = discord.ui.Container(accent_colour=discord.Colour(0x8B5CF6))
        where = _chapter_label(ev, chapter)
        container.add_item(
            discord.ui.TextDisplay(
                "## Multiple players found\n"
                f"More than one player matches **{discord.utils.escape_markdown(query)}**"
                + (f" in {where}'s chapter" if where else "")
                + ". Pick one below."
            )
        )
        options = [
            discord.SelectOption(
                label=str(c["name"])[:100],
                description=(
                    f"{REGION_LABELS.get(c['region'], c['region'])} · "
                    + (
                        f"No longer in T100, reached T{c['rank']}"
                        if c.get("history")
                        else (f"Final rank {c['rank']}" if c.get("ended") else f"Rank {c['rank']}")
                        + (f" · {int(c['points']):,} EP" if c.get("points") is not None else "")
                    )
                )[:100],
                value=str(i),
            )
            for i, c in enumerate(candidates)
        ]
        select = discord.ui.Select(placeholder="Which player?", options=options)
        select.callback = self._on_pick  # type: ignore[assignment]
        row = discord.ui.ActionRow()
        row.add_item(select)
        container.add_item(row)
        self.add_item(container)
        self._add_chapter_rows()

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        idx = int(interaction.data["values"][0])  # type: ignore[index,arg-type]
        card = await self.cog._build_player_card(
            self.candidates[idx],
            query=self.query,
            ev=self.ev,
            chapter=self.chapter,
            restrict_to=self.restrict_to or 0,
        )
        await interaction.response.edit_message(view=card)
        card.message = interaction.message
        self.stop()  # the card owns the message now; don't let this picker's timeout clobber it


class _PlayerNoMatchView(_ResearchView):
    """components-v2 "no player found" card for a chapter that yielded no match. chapter buttons
    underneath re-search the same query in another chapter (which may match, or show a picker)."""

    def __init__(
        self,
        cog: GraphCog,
        *,
        query: str,
        ev: "EventInfo | None",
        chapter: str | None,
        restrict_to: int,
    ) -> None:
        super().__init__(cog, query=query, ev=ev, chapter=chapter, restrict_to=restrict_to)
        container = discord.ui.Container(accent_colour=discord.Colour(0xE76A6A))
        where = _chapter_label(ev, chapter)
        multi = len(chapters_from_event(ev)) > 1
        body = (
            "## No player found\n"
            f"No player matching **{discord.utils.escape_markdown(query)}**"
            + (f" in {where}'s chapter" if where else "")
            + " on any region's top 100."
        )
        if multi:
            body += " Try another chapter below."
        container.add_item(discord.ui.TextDisplay(body))
        self.add_item(container)
        self._add_chapter_rows()


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(GraphCog(bot))
