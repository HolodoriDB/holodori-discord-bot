from __future__ import annotations

import asyncio
import io
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds, timezones
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS, autocompletes
from helpers.chapter_buttons import ChapterButtons, chapters_from_event
from helpers.views import HoloView
from services.graph import render_graph as _render_graph_img
from services.graph import render_heatmap as _render_heatmap_img
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from datetime import tzinfo

    from data.models import EventInfo
    from main import HolodoriBot

_TIER = (90, 150, 255, 255)
_USER = (120, 200, 130, 255)
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
        if tier_series:
            lines.append((f"Tier {tier} {label}", tier_series, _TIER))
        if user_series:
            lines.append((str(data.get("name") or f"#{tier}"), user_series, _USER))
        if not lines:
            return embeds.error_embed("No graph data for that event/tier yet."), []
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
        title = f"Tier {tier} {label} - {REGION_LABELS.get(region, region)}{suffix}"
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
        region = await self._region(interaction.user.id, region)
        tz_name = timezone or await self.bot.user_data.get_settings(interaction.user.id, "timezone")
        tz = timezones.resolve(tz_name)
        ev = await self._resolve_event(region, event)
        # default to the chapter live right now (else the latest)
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
        )
        # a view is worth showing when there's more than one chapter to switch between, a song
        # dropdown to offer, or a prediction to toggle (relay/EP events in total mode)
        chapters = chapters_from_event(ev)
        can_predict = (not music) and ev is not None and not ev.isSongScore
        if not files or (len(chapters) <= 1 and not songs and not can_predict):
            await interaction.followup.send(embed=embed, files=files)
            return
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
            restrict_to=interaction.user.id,
        )
        view.message = await interaction.followup.send(embed=embed, files=files, view=view, wait=True)

    @app_commands.command(
        name="heatmap",
        description="Hourly event point gain (EPH) heatmap for a player, or a tier's cutoff.",
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
        region = await self._region(interaction.user.id, region)
        tz_name = timezone or await self.bot.user_data.get_settings(interaction.user.id, "timezone")
        tz = timezones.resolve(tz_name)
        ev = await self._resolve_event(region, event)
        # default to the chapter live right now (else the latest)
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
        )
        chapters = chapters_from_event(ev)
        if not files or len(chapters) <= 1:
            await interaction.followup.send(embed=embed, files=files)
            return
        view = _HeatmapView(
            self,
            region=region,
            tier=tier,
            event_id=event,
            chapter=chapter,
            player_mode=player_mode,
            tz=tz,
            ev=ev,
            restrict_to=interaction.user.id,
        )
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
    ) -> tuple[discord.Embed, list[discord.File]]:
        assert self.bot.holo
        try:
            # player mode needs both the player series and the cutoff series (the latter is our
            # fetch coverage); a cutoff heatmap needs only the cutoff, so border=True skips the lookup
            data = await self.bot.holo.get_event_graph(
                region, tier, event_id=event_id, chapter_id=chapter, border=not player_mode
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

        presence: list[int] | None = None
        if player_mode:
            user_series = data.get("user") or []
            if len(user_series) < 2:
                return embeds.error_embed(f"No tracked player is at T{tier} for this event."), []
            value_series = user_series
            presence = [int(p[0]) for p in user_series]
            title = f"{data.get('name') or f'T{tier}'} EPH - {REGION_LABELS.get(region, region)}"
        else:
            value_series = tier_series
            title = f"Tier {tier} Cutoff EPH - {REGION_LABELS.get(region, region)}"

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
        region = await self._region(interaction.user.id, region)
        ev = await self._resolve_event(region, event)
        # default to the chapter live right now (else the latest)
        chapter = (ev.activeChapterId or (ev.chapters[-1] if ev.chapters else None)) if ev else None
        try:
            data = await self.bot.holo.get_event_graph(
                region, tier, event_id=event, chapter_id=chapter
            )
        except HolodoriError as e:
            msg = e.detail if e.status == 400 and e.detail else f"Couldn't fetch cutoff: {e.detail or e.status}"
            await interaction.followup.send(embed=embeds.error_embed(msg))
            return
        series = [(int(p[0]), float(p[1])) for p in (data.get("tier") or [])]
        if len(series) < 2:
            await interaction.followup.send(
                embed=embeds.error_embed("Not enough cutoff data for that event/tier yet.")
            )
            return

        last_ms, current = series[-1]
        start = data.get("startTime") or series[0][0]
        end = data.get("endTime") or last_ms
        elapsed_h = max((last_ms - start) / 3_600_000, 1 / 60)
        avg_hr = current / elapsed_h
        # gain over the last hour of data
        target = last_ms - 3_600_000
        prev = next((v for t, v in reversed(series) if t <= target), series[0][1])
        last_hr = current - prev
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
                f"**Last hour:** {int(last_hr):,}/hr"
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
            est_value = f"**Estimated final:** {int(pred['final']):,}{unit}"
            if pm:
                est_value += f" (± {int(pm):,})"
            lo, hi = pred.get("finalLow"), pred.get("finalHigh")
            if lo is not None and hi is not None:
                est_value += f"\n**Range:** {int(lo):,} to {int(hi):,}{unit}"
        elif data.get("songScore"):
            est_value = "Not available for song-score boards."
        else:
            est_value = "Too early to project a final yet."
        em.add_field(name="Point Estimation (Prediction)", value=est_value, inline=False)

        logo = self.bot.holo.unsquished_image_url(ev.logo) if ev and ev.logo else None
        if logo:
            em.set_thumbnail(url=logo)
        await interaction.followup.send(embed=em)


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
    """holomem chapter buttons (relay events) that re-render the heatmap for the picked chapter."""

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
        self._chapters = ChapterButtons(chapters_from_event(ev), chapter, self._on_chapter)
        self._chapters.add_to(self)

    async def _on_chapter(self, interaction: discord.Interaction, chapter_id: str) -> None:
        await interaction.response.defer()
        self.chapter = chapter_id
        self._chapters.set_current(chapter_id)
        embed, files = await self.cog.render_heatmap(
            region=self.region,
            tier=self.tier,
            event_id=self.event_id,
            chapter=chapter_id,
            player_mode=self.player_mode,
            tz=self.tz,
            ev=self.ev,
        )
        # keep the view even if this chapter has no data yet (so they can switch back)
        await interaction.edit_original_response(
            embed=embed, attachments=files, view=self
        )


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(GraphCog(bot))
