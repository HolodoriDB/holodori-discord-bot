from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import details, embeds, imaging, timezones
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS, autocompletes
from helpers.views import HoloView
from services.graph import render_graph
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
        suffix = f" - {song_title}" if music and song_title else ""
        title = f"Tier {tier} {label} - {REGION_LABELS.get(region, region)}{suffix}"
        img = await asyncio.to_thread(render_graph, lines, title, tz=tz)

        embed = embeds.embed(title=title)
        last_ms = max((int(p[0]) for _, s, _ in lines for p in s), default=0)
        if last_ms:
            embed.description = f"**Last Data Update:** <t:{last_ms // 1000}:R>"
        files = [discord.File(io.BytesIO(img), "graph.png")]
        logo_bytes = (
            await details.unsquished_bytes(self.bot, ev.logo, imaging.ASPECT_LOGO)
            if ev and ev.logo
            else None
        )
        if logo_bytes:
            files.append(discord.File(io.BytesIO(logo_bytes), "logo.png"))
            embed.set_thumbnail(url="attachment://logo.png")
        embed.set_image(url="attachment://graph.png")
        return embed, files

    @graph.command(name="cutoff", description="Graph a tier's cutoff over the event.")
    @app_commands.describe(
        tier="Tier rank to graph (e.g. 100).",
        region="Game server region.",
        event="Event (defaults to the latest).",
        mode="Total event score or per-song scores.",
        border="Use the tier border line.",
        timezone="Timezone for the time axis (defaults to your setting).",
    )
    @app_commands.choices(region=REGION_CHOICES, mode=_MODE_CHOICES)
    @app_commands.autocomplete(event=autocompletes.event())
    async def cutoff(
        self,
        interaction: discord.Interaction,
        tier: int,
        region: str = "default",
        event: str | None = None,
        mode: str = "total",
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
        ev = await self._resolve_event(region, event)
        chapter = ev.chapters[-1] if ev and ev.chapters else None  # follow the latest chapter
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
        )
        if not files or not songs:
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
            songs=songs,
            music_id=music_id,
            restrict_to=interaction.user.id,
        )
        view.message = await interaction.followup.send(embed=embed, files=files, view=view, wait=True)


class _GraphView(HoloView):
    """song dropdown for the Song-Scores graph mode; re-renders on selection."""

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
        songs: list[tuple[str, str]],
        music_id: str | None,
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
        self.music_id = music_id
        select = discord.ui.Select(
            placeholder="Choose a song...",
            options=[
                discord.SelectOption(label=title[:100], value=mid, default=mid == music_id)
                for mid, title in songs[:25]
            ],
        )
        self._songs = dict(songs)
        select.callback = self._on_song  # type: ignore[method-assign]
        self.add_item(select)

    async def _on_song(self, interaction: discord.Interaction) -> None:
        self.music_id = interaction.data["values"][0]  # type: ignore[index]
        await interaction.response.defer()
        embed, files = await self.cog.render_graph(
            region=self.region,
            tier=self.tier,
            event_id=self.event_id,
            chapter=self.chapter,
            border=self.border,
            music=True,
            music_id=self.music_id,
            song_title=self._songs.get(self.music_id),
            tz=self.tz,
            ev=self.ev,
        )
        await interaction.edit_original_response(embed=embed, attachments=files, view=self)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(GraphCog(bot))
