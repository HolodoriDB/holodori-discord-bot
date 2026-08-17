from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import embeds
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS
from services.holodori import HolodoriError
from services.leaderboard import LBRow, format_leaderboard

if TYPE_CHECKING:
    from main import HolodoriBot

# event ranking trackers: a channel wide live leaderboard poster (/tracking) and per tier / per
# player alert pings (/track). one poll loop fetches each region's board once per tick and serves
# every subscriber. following a specific player needs the stable userId the authorized leaderboard
# route returns (the public one omits it).

_TOP = 20  # rows in a channel leaderboard post
_POLL_MINUTES = 2
_HOURLY_SECONDS = 3540  # ~59 min, so the 2 min tick lands hourly posts without double-firing near hour boundaries
_TIME_CHOICES = [
    app_commands.Choice(name="2 minutes", value=2),
    app_commands.Choice(name="1 hour", value=60),
]
_BIG = 10**15  # effectively unbounded max gain


def _t20_rows(rankings: list[dict]) -> list[LBRow]:
    out: list[LBRow] = []
    for r in rankings[:_TOP]:
        ep = r.get("epChange")
        out.append(
            LBRow(
                rank=int(r.get("rank") or 0),
                name=str(r.get("name") or "?"),
                score=int(r.get("score") or 0),
                rank_change=int(r.get("rankChange") or 0),
                score_change=int(ep) if ep is not None else None,
            )
        )
    return out


class TrackerCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    async def _region(self, user_id: int, region: str) -> str:
        assert self.bot.user_data
        if region == "default":
            return await self.bot.user_data.get_settings(user_id, "default_region")
        return region

    # --- rendering / posting ---

    async def _post_board(self, channel: discord.abc.Messageable, data: dict) -> None:
        rankings = data.get("rankings") or []
        if not rankings:
            return
        embed = embeds.embed(title=data.get("eventName") or "Event Leaderboard")
        parts: list[str] = []
        if data.get("final"):  # the event's aggregation has finished
            parts.append("**EVENT HAS ENDED**")
        # lastMinute is the last snapshot time in epoch ms; updatedAt is an iso string, not a number
        updated = data.get("lastMinute")
        if isinstance(updated, (int, float)):
            parts.append(f"Top {_TOP}, updated <t:{int(updated) // 1000}:R>")
        parts.append(format_leaderboard(_t20_rows(rankings)))
        embed.description = "\n".join(parts)
        logo = self.bot.holo.unsquished_image_url(data.get("logo")) if self.bot.holo else None
        if logo:
            embed.set_thumbnail(url=logo)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            # the usual single-channel stall: lost Send Messages / Embed Links, or the channel was
            # deleted. surface it instead of going silently quiet
            self.bot.warn(f"[tracker] post to channel {getattr(channel, 'id', '?')} failed: {e!r}")

    async def _ping(self, channel: discord.abc.Messageable, content: str) -> None:
        try:
            await channel.send(content)
        except discord.HTTPException:
            pass

    # --- poll loop ---

    @tasks.loop(minutes=_POLL_MINUTES)
    async def poll(self) -> None:
        if not (self.bot.user_data and self.bot.holo):
            return
        try:
            trackers = await self.bot.user_data.list_event_trackers()
            alerts = await self.bot.user_data.list_track_alerts()
        except Exception as e:
            # this aborts the WHOLE poll (every tracker), so it must never fail silently - e.g. a
            # missing column after a schema change that wasn't migrated
            self.bot.warn(f"[tracker] poll aborted, couldn't load trackers/alerts: {e!r}")
            return
        regions = {t["region"] for t in trackers} | {a["region"] for a in alerts}
        now = time.time()
        ended_notified: set[int] = set()  # channels already told "event ended" this tick

        for region in regions:
            try:
                data = await self.bot.holo.get_event_leaderboard_ids(region)
            except (HolodoriError, Exception) as e:
                # a region-wide fetch failure stalls every tracker in that region (not just one)
                self.bot.warn(f"[tracker] leaderboard fetch for region {region} failed: {e!r}")
                continue
            rankings = data.get("rankings") or []
            event_id = data.get("eventId")
            final = bool(data.get("final"))
            by_rank = {int(r["rank"]): r for r in rankings if r.get("rank") is not None}
            by_user = {str(r["userId"]): r for r in rankings if r.get("userId") is not None}

            for t in trackers:
                if t["region"] != region:
                    continue
                channel = self.bot.get_channel(t["channel_id"])
                if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                    # None = deleted, archived thread, or the bot can no longer see it
                    self.bot.warn(
                        f"[tracker] channel {t['channel_id']} (region {region}) unavailable; skipping"
                    )
                    continue
                if t["tracking_type"] == 2:
                    await self._post_board(channel, data)
                elif now - (t.get("last_post") or 0) >= _HOURLY_SECONDS:
                    await self.bot.user_data.set_tracker_last_post(t["channel_id"], now)
                    await self._post_board(channel, data)

            for a in alerts:
                if a["region"] == region:
                    await self._process_alert(a, event_id, final, by_rank, by_user, ended_notified)

    @poll.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_alert(
        self,
        a: dict,
        event_id: str | None,
        final: bool,
        by_rank: dict,
        by_user: dict,
        ended_notified: set[int],
    ) -> None:
        assert self.bot.user_data
        channel = self.bot.get_channel(a["channel_id"])
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return
        # a tracker is bound to one event (songs and tiers differ every event), so when that event
        # ends (rolled over to a new one, or gone final) it can never fire again: tell the channel
        # once, then remove it
        rolled = bool(a.get("event_id")) and bool(event_id) and a["event_id"] != event_id
        finished = a.get("event_id") == event_id and final
        if rolled or finished:
            if a["channel_id"] not in ended_notified:
                ended_notified.add(a["channel_id"])
                try:
                    await channel.send(
                        embed=embeds.embed(
                            description="The event has ended. Tracking has automatically been disabled."
                        )
                    )
                except discord.HTTPException:
                    pass
            await self.bot.user_data.remove_track_alert(a["id"])
            return
        cfg = a["config"]
        mention = f"<@{a['user_id']}>"

        if a["kind"] == "tier":
            row = by_rank.get(int(cfg.get("tier", 0)))
            if not row or row.get("score") is None:
                return
            score, target = int(row["score"]), int(cfg.get("target", 0))
            if score >= target:
                await self._ping(
                    channel,
                    f"{mention} T{cfg.get('tier')} moved, now at {score:,} EP. You tracked {target:,}.",
                )
                await self.bot.user_data.remove_track_alert(a["id"])
            return

        # kind == "user": follow the player by their stable id
        row = by_user.get(str(cfg.get("followed")))
        if not row or row.get("score") is None:
            return
        score, rank = int(row["score"]), row.get("rank")
        name = cfg.get("name") or f"#{rank}"
        changed = False
        cutoff = cfg.get("cutoff")
        if cutoff and score >= int(cutoff):
            await self._ping(
                channel, f"T{rank} {name} passed the cutoff {int(cutoff):,}, now at {score:,} EP."
            )
            cfg["cutoff"] = None
            changed = True
        change = score - int(cfg.get("last_score", score))
        if change != 0:
            lo, hi = int(cfg.get("min", 1)), int(cfg.get("max", _BIG))
            if lo <= change <= hi:
                await self._ping(
                    channel, f"T{rank} {name} had a game worth {change:,} EP. Current: {score:,} EP."
                )
            cfg["last_score"] = score
            changed = True
        if changed:
            await self.bot.user_data.update_track_alert(a["id"], cfg)

    # --- /tracking (channel wide live leaderboard, admin only) ---

    @app_commands.command(
        name="tracking", description="Post a live event leaderboard to a channel on a timer."
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Where updates are posted.",
        time="How often it updates.",
        enable="Turn the tracker on or off.",
        region="Event region.",
    )
    @app_commands.choices(time=_TIME_CHOICES, region=REGION_CHOICES)
    async def tracking(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        time: app_commands.Choice[int],
        enable: bool,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self.bot.user_data and interaction.guild
        if not enable:
            removed = await self.bot.user_data.remove_event_tracker(channel.id)
            await interaction.followup.send(
                embed=embeds.success_embed(f"Tracking disabled for {channel.mention}.")
                if removed
                else embeds.error_embed("That channel has no tracker.")
            )
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                embed=embeds.error_embed("I need Send Messages and Embed Links in that channel.")
            )
            return
        reg = await self._region(interaction.user.id, region)
        await self.bot.user_data.set_event_tracker(channel.id, interaction.guild.id, reg, time.value)
        # post one board immediately so the channel isn't blank until the first tick; stamp last_post
        # so an hourly tracker doesn't double-fire on the next poll (note: `time` here is the Choice,
        # not the module, so use discord's clock)
        if self.bot.holo:
            try:
                data = await self.bot.holo.get_event_leaderboard_ids(reg)
                await self._post_board(channel, data)
                await self.bot.user_data.set_tracker_last_post(
                    channel.id, discord.utils.utcnow().timestamp()
                )
            except Exception:
                pass
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"**Channel:** {channel.mention}\n**Region:** {REGION_LABELS.get(reg, reg)}\n"
                f"**Every:** {time.name}\n**Status:** Enabled"
            )
        )

    # --- /track (per tier / per player alerts) ---

    track = app_commands.Group(
        name="track",
        description="Get pinged when a tier's cutoff moves or a player makes progress.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=False, private_channel=False
        ),
    )

    @track.command(name="tier", description="Ping when a tier's cutoff moves or passes a value.")
    @app_commands.describe(
        tier="Leaderboard tier (1-100).",
        cutoff="Optional EP to alert at (defaults to any movement).",
        region="Event region.",
    )
    @app_commands.choices(region=REGION_CHOICES)
    async def track_tier(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Range[int, 1, 100],
        cutoff: int | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and self.bot.holo and interaction.guild and interaction.channel
        reg = await self._region(interaction.user.id, region)
        try:
            data = await self.bot.holo.get_event_leaderboard(reg)
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch the leaderboard: {e.detail or e.status}")
            )
            return
        event_id = data.get("eventId")
        row = next((r for r in (data.get("rankings") or []) if int(r.get("rank", 0)) == tier), None)
        if row is None or not event_id:
            await interaction.followup.send(
                embed=embeds.error_embed("No live event, or that tier isn't on the board yet.")
            )
            return
        target = cutoff if cutoff is not None else int(row.get("score") or 0) + 1
        await self.bot.user_data.add_track_alert(
            interaction.guild.id, interaction.channel.id, interaction.user.id, reg, event_id,
            "tier", {"tier": tier, "target": target},
        )
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Tracking **T{tier}** ({REGION_LABELS.get(reg, reg)}). I'll ping you when it reaches "
                f"`{target:,}` EP."
            )
        )

    @track.command(name="user", description="Follow the player at a tier and alert on their progress.")
    @app_commands.describe(
        tier="The player's current tier (1-100).",
        cutoff="Alert once when they pass this EP.",
        min="Only alert on per-game gains at or above this.",
        max="Only alert on per-game gains at or below this.",
        region="Event region.",
    )
    @app_commands.choices(region=REGION_CHOICES)
    async def track_user(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Range[int, 1, 100],
        cutoff: int | None = None,
        min: int | None = None,
        max: int | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and self.bot.holo and interaction.guild and interaction.channel
        reg = await self._region(interaction.user.id, region)
        try:
            data = await self.bot.holo.get_event_leaderboard_ids(reg)
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch the leaderboard: {e.detail or e.status}")
            )
            return
        event_id = data.get("eventId")
        row = next((r for r in (data.get("rankings") or []) if int(r.get("rank", 0)) == tier), None)
        uid = row.get("userId") if row else None
        if not row or not uid or not event_id:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "Couldn't find a player at that tier, or player ids aren't available."
                )
            )
            return
        name = row.get("name") or f"#{tier}"
        cfg = {
            "tier": tier,
            "followed": str(uid),
            "name": name,
            "cutoff": cutoff,
            "min": min if min is not None else 1,
            "max": max if max is not None else _BIG,
            "last_score": int(row.get("score") or 0),
        }
        await self.bot.user_data.add_track_alert(
            interaction.guild.id, interaction.channel.id, interaction.user.id, reg, event_id,
            "user", cfg,
        )
        await interaction.followup.send(
            embed=embeds.success_embed(f"Following **{name}** at T{tier} ({REGION_LABELS.get(reg, reg)}).")
        )

    def _visible(self, interaction: discord.Interaction, alerts: list[dict]) -> list[dict]:
        member = interaction.user
        is_admin = isinstance(member, discord.Member) and member.guild_permissions.manage_guild
        return alerts if is_admin else [a for a in alerts if a["user_id"] == interaction.user.id]

    @track.command(name="list", description="List the trackers in this server.")
    async def track_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and interaction.guild
        mine = self._visible(interaction, await self.bot.user_data.list_track_alerts(interaction.guild.id))
        if not mine:
            await interaction.followup.send(embed=embeds.embed(description="No trackers here."))
            return
        lines = []
        for i, a in enumerate(mine, 1):
            c, reg = a["config"], REGION_LABELS.get(a["region"], a["region"])
            if a["kind"] == "tier":
                lines.append(f"`{i}.` **T{c.get('tier')}** at `{int(c.get('target', 0)):,}` EP ({reg})")
            else:
                lines.append(f"`{i}.` **{c.get('name')}** at T{c.get('tier')} ({reg})")
        await interaction.followup.send(embed=embeds.embed(title="Trackers", description="\n".join(lines)))

    @track.command(name="remove", description="Remove a tracker by its number from /track list.")
    @app_commands.describe(num="The number shown in /track list.")
    async def track_remove(self, interaction: discord.Interaction, num: int) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and interaction.guild
        mine = self._visible(interaction, await self.bot.user_data.list_track_alerts(interaction.guild.id))
        if not 1 <= num <= len(mine):
            await interaction.followup.send(embed=embeds.error_embed("No tracker with that number."))
            return
        await self.bot.user_data.remove_track_alert(mine[num - 1]["id"])
        await interaction.followup.send(embed=embeds.success_embed(f"Removed tracker #{num}."))


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(TrackerCog(bot))
