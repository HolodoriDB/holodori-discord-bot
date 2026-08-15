from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import autocompletes
from helpers.views import HoloView

if TYPE_CHECKING:
    from main import HolodoriBot

# per channel co-op room waitlist. users join/leave a queue, set the song, mark when they expect to
# leave, and ping the next person. state is persisted to psql so a restart keeps every queue.

_ACTIVE_WINDOW = 3600.0  # a waitlist counts as "active" (for list / joinall / clear) for one hour
_CONFIRM_SECONDS = 60
_CANCEL_SECONDS = 180


class WaitlistButtons(discord.ui.View):
    # persistent (custom_id + no timeout): survives restarts once registered via bot.add_view. the
    # buttons act on whatever channel they're clicked in, so one registered instance serves them all
    def __init__(self, cog: WaitlistCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Join", style=discord.ButtonStyle.primary, custom_id="waitlist:join")
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.on_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger, custom_id="waitlist:leave")
    async def leave(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.on_leave(interaction)

    @discord.ui.button(label="Ping Next", style=discord.ButtonStyle.secondary, custom_id="waitlist:ping")
    async def ping(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.on_ping(interaction)


class _ConfirmJoin(HoloView):
    def __init__(self, cog: WaitlistCog, channel_id: int, next_user: int, guild_id: int) -> None:
        super().__init__(timeout=_CONFIRM_SECONDS, restrict_to=next_user)
        self.cog = cog
        self.channel_id = channel_id
        self.next_user = next_user
        self.guild_id = guild_id
        self.done = False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✔️")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        assert self.cog.bot.user_data
        self.done = True
        await self.cog.bot.user_data.remove_user_all_waitlists(self.next_user)
        await interaction.response.edit_message(
            content=(
                f"<@{self.next_user}> confirmed and joined the room. "
                "Refresh the waitlist to see the updated queue."
            ),
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        if self.done:
            return
        assert self.cog.bot.user_data
        wl = await self.cog.bot.user_data.get_waitlist(self.channel_id)
        if wl and self.next_user in wl["users"]:
            wl["users"].remove(self.next_user)
            await self.cog.save(self.channel_id, wl.get("guild_id") or self.guild_id, wl)
        if self.message:
            try:
                await self.message.edit(
                    content=f"<@{self.next_user}> did not confirm in time and was removed from this waitlist.",
                    view=None,
                )
            except discord.HTTPException:
                pass


class _CancelClear(HoloView):
    def __init__(self, cog: WaitlistCog, channel_id: int) -> None:
        super().__init__(timeout=_CANCEL_SECONDS)
        self.cog = cog
        self.channel_id = channel_id
        self.cancelled = False

    @discord.ui.button(label="Cancel Clear", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.cancelled = True
        await interaction.response.edit_message(
            embed=embeds.embed(description="Waitlist clear cancelled."), view=None
        )
        self.stop()

    async def on_timeout(self) -> None:
        if self.cancelled or not self.cog.bot.user_data:
            return
        await self.cog.bot.user_data.clear_waitlist(self.channel_id)
        if self.message:
            try:
                await self.message.edit(embed=embeds.success_embed("Waitlist cleared."), view=None)
            except discord.HTTPException:
                pass


class WaitlistCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(WaitlistButtons(self))

    # --- state helpers ---

    @staticmethod
    def _prune(leavers: dict) -> dict:
        now = time.time()
        return {u: t for u, t in leavers.items() if t > now}

    def _blank(self, guild_id: int) -> dict:
        return {
            "guild_id": guild_id,
            "song": None,
            "users": [],
            "leavers": {},
            "message_id": None,
            "last_use": time.time(),
        }

    async def save(self, channel_id: int, guild_id: int, wl: dict) -> None:
        assert self.bot.user_data
        await self.bot.user_data.save_waitlist(
            channel_id,
            guild_id,
            song=wl.get("song"),
            users=wl["users"],
            leavers=wl.get("leavers") or {},
            message_id=wl.get("message_id"),
            last_use=time.time(),
        )

    def _embed(self, wl: dict) -> discord.Embed:
        users = wl.get("users") or []
        song = wl.get("song")
        leavers = self._prune(wl.get("leavers") or {})
        em = embeds.embed(title="Waitlist Queue")
        if song:
            em.description = f"Song: {song}"
        if leavers:
            em.add_field(
                name="Leaving Soon",
                value="\n".join(f"<@{u}> <t:{int(t)}:R>" for u, t in leavers.items()),
                inline=False,
            )
        em.add_field(
            name="Waitlist Users",
            value="\n".join(f"<@{u}>" for u in users) if users else "No users in queue",
            inline=False,
        )
        return em

    async def _refresh(self, interaction: discord.Interaction) -> None:
        # post a fresh waitlist message and delete the previous one, like a live-updating pin
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        cid = interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(cid) or self._blank(interaction.guild_id)
        wl["leavers"] = self._prune(wl.get("leavers") or {})
        old_id = wl.get("message_id")
        msg = await interaction.followup.send(embed=self._embed(wl), view=WaitlistButtons(self), wait=True)
        wl["message_id"] = msg.id
        await self.save(cid, interaction.guild_id, wl)
        if old_id and old_id != msg.id and isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            try:
                await (await interaction.channel.fetch_message(old_id)).delete()
            except discord.HTTPException:
                pass

    # --- button handlers ---

    async def on_join(self, interaction: discord.Interaction) -> None:
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        cid = interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(cid) or self._blank(interaction.guild_id)
        uid = interaction.user.id
        if uid in wl["users"]:
            await interaction.response.send_message("You are already in the waitlist.", ephemeral=True)
            return
        wl["users"].append(uid)
        wl["leavers"] = self._prune(wl.get("leavers") or {})
        await self.save(cid, interaction.guild_id, wl)
        await interaction.response.edit_message(embed=self._embed(wl), view=WaitlistButtons(self))
        await interaction.followup.send(
            f"<@{uid}> joined the waitlist.", allowed_mentions=discord.AllowedMentions.none()
        )

    async def on_leave(self, interaction: discord.Interaction) -> None:
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        cid = interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(cid)
        if wl is None:
            await interaction.response.defer()
            return
        uid = interaction.user.id
        if uid in wl["users"]:
            wl["users"].remove(uid)
            wl["leavers"] = self._prune(wl.get("leavers") or {})
            await self.save(cid, interaction.guild_id, wl)
        await interaction.response.edit_message(embed=self._embed(wl), view=WaitlistButtons(self))

    async def on_ping(self, interaction: discord.Interaction) -> None:
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(interaction.channel_id)
        if not wl or not wl["users"]:
            await interaction.response.send_message("No users in the queue.", ephemeral=True)
            return
        next_user = wl["users"][0]
        view = _ConfirmJoin(self, interaction.channel_id, next_user, wl.get("guild_id") or interaction.guild_id)
        await interaction.response.send_message(
            content=(
                f"<@{next_user}>, you are up next. Press Confirm to join the room. This also removes "
                f"you from every other waitlist.\n(requested by <@{interaction.user.id}>)"
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    # --- slash commands ---

    waitlist = app_commands.Group(
        name="waitlist",
        description="A co-op room waitlist queue for this channel.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=False, private_channel=False
        ),
    )

    @waitlist.command(name="show", description="Show or refresh the waitlist for this channel.")
    async def show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._refresh(interaction)

    @waitlist.command(name="remove", description="Remove a user from this channel's waitlist.")
    @app_commands.describe(user="The user to remove.")
    async def remove(self, interaction: discord.Interaction, user: discord.User) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(interaction.channel_id)
        if wl and user.id in wl["users"]:
            wl["users"].remove(user.id)
            await self.save(interaction.channel_id, interaction.guild_id, wl)
        await self._refresh(interaction)

    @waitlist.command(name="clear", description="Clear this channel's waitlist.")
    async def clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(interaction.channel_id)
        if not wl or (time.time() - (wl.get("last_use") or 0)) > _ACTIVE_WINDOW:
            await self.bot.user_data.clear_waitlist(interaction.channel_id)
            await interaction.followup.send(embed=embeds.success_embed("Waitlist cleared."))
            return
        view = _CancelClear(self, interaction.channel_id)
        view.message = await interaction.followup.send(
            embed=embeds.warn_embed("Clear requested. Press Cancel within 3 minutes to stop it."),
            view=view,
            wait=True,
        )

    @waitlist.command(name="leave", description="Remove yourself from every waitlist.")
    async def leave(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self.bot.user_data
        await self.bot.user_data.remove_user_all_waitlists(interaction.user.id)
        await interaction.followup.send(
            embed=embeds.success_embed("You have been removed from all waitlists.")
        )

    def _song_title(self, song: str) -> str:
        match = self.bot.data.match_song(song) if self.bot.data else None
        return match.title if match else song

    @waitlist.command(name="song", description="Set the song for this channel's waitlist.")
    @app_commands.describe(song="The song.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def song(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(interaction.channel_id) or self._blank(interaction.guild_id)
        wl["song"] = self._song_title(song)
        await self.save(interaction.channel_id, interaction.guild_id, wl)
        await self._refresh(interaction)

    @waitlist.command(name="leaving", description="Set when you expect to leave the waitlist.")
    @app_commands.describe(minutes="Minutes from now you expect to leave.")
    async def leaving(
        self, interaction: discord.Interaction, minutes: app_commands.Range[int, 0, 1440]
    ) -> None:
        await interaction.response.defer()
        assert self.bot.user_data and interaction.guild_id and interaction.channel_id
        wl = await self.bot.user_data.get_waitlist(interaction.channel_id) or self._blank(interaction.guild_id)
        wl.setdefault("leavers", {})[str(interaction.user.id)] = int(time.time()) + minutes * 60
        await self.save(interaction.channel_id, interaction.guild_id, wl)
        await self._refresh(interaction)

    @waitlist.command(
        name="joinall",
        description="Join every waitlist in this server for a song (active within the last hour).",
    )
    @app_commands.describe(song="The song to join waitlists for.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def joinall(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self.bot.user_data and interaction.guild_id
        title = self._song_title(song)
        since = time.time() - _ACTIVE_WINDOW
        found = await self.bot.user_data.list_waitlists(interaction.guild_id, song=title, since=since)
        uid = interaction.user.id
        joined: list[int] = []
        for row in found:
            if uid in row["users"]:
                continue
            full = await self.bot.user_data.get_waitlist(row["channel_id"])
            if full and uid not in full["users"]:
                full["users"].append(uid)
                await self.save(row["channel_id"], full["guild_id"], full)
                joined.append(row["channel_id"])
        if joined:
            channels = "\n".join(f"<#{c}>" for c in joined)
            await interaction.followup.send(
                embed=embeds.success_embed(f"Joined {len(joined)} waitlist(s) for **{title}**:\n{channels}")
            )
        else:
            await interaction.followup.send(
                embed=embeds.embed(description=f"No active waitlists found for **{title}**.")
            )

    @waitlist.command(name="list", description="List the active waitlists in this server.")
    @app_commands.describe(song="Optionally filter by song.")
    @app_commands.autocomplete(song=autocompletes.song())
    async def list(self, interaction: discord.Interaction, song: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self.bot.user_data and interaction.guild_id
        title = self._song_title(song) if song else None
        since = time.time() - _ACTIVE_WINDOW
        found = await self.bot.user_data.list_waitlists(interaction.guild_id, song=title, since=since)
        if not found:
            await interaction.followup.send(embed=embeds.embed(description="No active waitlists found."))
            return
        lines = [
            f"<#{wl['channel_id']}> <t:{int(wl['last_use'])}:R> {len(wl['users'])} in queue "
            f"`{wl['song'] or 'No song set'}`"
            for wl in found[:25]
        ]
        await interaction.followup.send(embed=embeds.embed(title="Waitlists", description="\n".join(lines)))


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(WaitlistCog(bot))
