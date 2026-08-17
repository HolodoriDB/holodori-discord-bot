"""manage search aliases for songs / events / holomems (alias managers only).

the backend is the source of truth (bot-only, internal-token gated); each edit also updates the
data layer's local copy at once so it's current without waiting for the 120s poll. NOTE: nothing
matches against these aliases yet - this only stores and mirrors them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data import search
from data.search import preprocess
from helpers import embeds
from helpers.autocompletes import autocompletes
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

_REGIONS = ("us", "as", "jp")


class AliasCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    # --- permission (owners always; otherwise a manager role in the support server) ---

    async def _is_alias_mod(self, user_id: int) -> bool:
        if user_id in (self.bot.owner_ids or set()):
            return True
        role_ids = set(self.bot.config["discord"].get("alias_manager_role_ids", []))
        support_id = self.bot.config["discord"].get("support_id")
        if not role_ids or not support_id:
            return False
        guild = self.bot.get_guild(support_id)
        if not guild:
            return False
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return False
        return bool(role_ids & {r.id for r in member.roles})

    async def _deny(self, interaction: discord.Interaction) -> bool:
        """Reply and return True if this caller may not use the alias commands."""
        if await self._is_alias_mod(interaction.user.id):
            return False
        await interaction.response.send_message(
            embed=embeds.error_embed("You're not authorized to manage aliases."), ephemeral=True
        )
        return True

    # --- target resolution (by real name/id; aliases are NOT used here) ---

    async def _events(self) -> list:
        assert self.bot.data and self.bot.holo
        # events share their id across regions, so dedupe by eventId
        seen: dict[str, object] = {}
        for region in _REGIONS:
            try:
                for e in await self.bot.data.events(region, self.bot.holo.lang):
                    seen.setdefault(e.eventId, e)
            except Exception:
                continue
        return list(seen.values())

    async def _resolve(self, kind: str, query: str) -> tuple[str, str] | None:
        """(target_id, display name) for the song/event/holomem matching `query`, or None."""
        assert self.bot.data
        data = self.bot.data
        if kind == "song":
            m = data.get_song(query) or data.match_song(query)
            return (m.id, m.title) if m else None
        if kind == "holomem":
            m = data.get_holomem(query) or data.match_holomem(query)
            return (m.id, m.name) if m else None
        events = await self._events()
        ev = next((e for e in events if e.eventId == query), None) or search.best_match(
            query, events, lambda e: (e.eventId, e.name)
        )
        # events have no real name (only a generic label), so the id is the identifier we show
        return (ev.eventId, ev.eventId) if ev else None

    def _local_aliases(self, kind: str, target_id: str) -> list[str]:
        assert self.bot.data
        data = self.bot.data
        getter = {
            "song": data.song_aliases,
            "event": data.event_aliases,
            "holomem": data.holomem_aliases,
        }[kind]
        return getter(target_id)

    async def _holder_name(self, kind: str, target_id: str) -> str:
        assert self.bot.data
        # nice name for the song/event/holomem that already owns a taken alias, else its id
        if kind == "song":
            m = self.bot.data.get_song(target_id)
            return f"**{m.title}** (`{target_id}`)" if m else f"song `{target_id}`"
        if kind == "holomem":
            m = self.bot.data.get_holomem(target_id)
            return f"**{m.name}** (`{target_id}`)" if m else f"holomem `{target_id}`"
        return f"event `{target_id}`"  # events are identified by id, not a name

    async def _add_error(self, error: HolodoriError, alias: str, kind: str) -> discord.Embed:
        assert self.bot.holo
        # aliases are unique within a kind; name the one already holding it (structured 409 detail,
        # or fall back to looking the holder up ourselves)
        holder = error.data.get("target_id")
        if holder is None and error.status in (409, 500):
            try:
                existing = await self.bot.holo.get_aliases(kind)
                hit = next((a for a in existing if a.alias == alias), None)
                holder = hit.target_id if hit else None
            except HolodoriError:
                holder = None
        if holder is not None:
            return embeds.error_embed(
                f"`{alias}` is already an alias for {await self._holder_name(kind, holder)}.\n"
                "Remove it from there before adding it here.",
                title="Alias already taken",
            )
        return embeds.error_embed(f"Couldn't add alias: {error.detail or error.status}")

    # --- shared command bodies ---

    async def _do_list(self, interaction: discord.Interaction, kind: str, query: str) -> None:
        if await self._deny(interaction):
            return
        assert self.bot.data and self.bot.holo
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve(kind, query)
        if not resolved:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a {kind} matching `{query}`.")
            )
            return
        target_id, name = resolved
        names = sorted(self._local_aliases(kind, target_id))
        embed = embeds.embed(
            title=f"Aliases - {name}",
            description="\n".join(f"- `{n}`" for n in names) if names else "No aliases yet.",
        )
        embed.set_footer(text=f"{kind} {target_id} - {len(names)} aliases")
        await interaction.followup.send(embed=embed)

    async def _do_add(
        self, interaction: discord.Interaction, kind: str, query: str, alias: str
    ) -> None:
        if await self._deny(interaction):
            return
        assert self.bot.data and self.bot.holo
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve(kind, query)
        if not resolved:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a {kind} matching `{query}`.")
            )
            return
        target_id, name = resolved
        target = preprocess(alias)
        if not target:
            await interaction.followup.send(
                embed=embeds.error_embed("That alias is empty after normalization.")
            )
            return
        try:
            await self.bot.holo.add_alias(kind, target_id, target)
        except HolodoriError as e:
            await interaction.followup.send(embed=await self._add_error(e, target, kind))
            return
        self.bot.data.add_alias_local(kind, target_id, target)
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Added alias for **{name}** (`{target_id}`)\nAlias: `{target}`",
                title="Added alias!",
            )
        )

    async def _do_remove(
        self, interaction: discord.Interaction, kind: str, query: str, alias: str
    ) -> None:
        if await self._deny(interaction):
            return
        assert self.bot.data and self.bot.holo
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve(kind, query)
        if not resolved:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a {kind} matching `{query}`.")
            )
            return
        target_id, name = resolved
        target = preprocess(alias)  # stored normalized, so normalize the input the same way
        try:
            existing = await self.bot.holo.get_aliases(kind)
            match = next(
                (a for a in existing if a.target_id == target_id and a.alias == target), None
            )
            if not match:
                await interaction.followup.send(
                    embed=embeds.error_embed(f"No alias `{target}` on **{name}**.")
                )
                return
            await self.bot.holo.remove_alias(kind, match.id)
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't remove alias: {e.detail or e.status}")
            )
            return
        self.bot.data.remove_alias_local(kind, target_id, target)
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Removed alias for **{name}** (`{target_id}`)\nAlias: `{target}`",
                title="Removed alias!",
            )
        )

    # --- command groups ---

    alias = app_commands.Group(
        name="alias",
        description="Manage search aliases (alias managers only).",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )
    alias_song = app_commands.Group(name="song", description="Song aliases.", parent=alias)
    alias_event = app_commands.Group(name="event", description="Event aliases.", parent=alias)
    alias_holomem = app_commands.Group(name="holomem", description="Holomem aliases.", parent=alias)

    # song
    @alias_song.command(name="list", description="Authorized only; view a song's aliases.")
    @app_commands.autocomplete(song=autocompletes.song())
    @app_commands.describe(song="Song name or ID.")
    async def song_list(self, interaction: discord.Interaction, song: str) -> None:
        await self._do_list(interaction, "song", song)

    @alias_song.command(name="add", description="Authorized only; add a song alias.")
    @app_commands.autocomplete(song=autocompletes.song())
    @app_commands.describe(song="Song name or ID.", alias="Alias to add.")
    async def song_add(self, interaction: discord.Interaction, song: str, alias: str) -> None:
        await self._do_add(interaction, "song", song, alias)

    @alias_song.command(name="remove", description="Authorized only; remove a song alias.")
    @app_commands.autocomplete(song=autocompletes.song())
    @app_commands.describe(song="Song name or ID.", alias="Alias to remove.")
    async def song_remove(self, interaction: discord.Interaction, song: str, alias: str) -> None:
        await self._do_remove(interaction, "song", song, alias)

    # event
    @alias_event.command(name="list", description="Authorized only; view an event's aliases.")
    @app_commands.autocomplete(event=autocompletes.event_id())
    @app_commands.describe(event="Event name or ID.")
    async def event_list(self, interaction: discord.Interaction, event: str) -> None:
        await self._do_list(interaction, "event", event)

    @alias_event.command(name="add", description="Authorized only; add an event alias.")
    @app_commands.autocomplete(event=autocompletes.event_id())
    @app_commands.describe(event="Event name or ID.", alias="Alias to add.")
    async def event_add(self, interaction: discord.Interaction, event: str, alias: str) -> None:
        await self._do_add(interaction, "event", event, alias)

    @alias_event.command(name="remove", description="Authorized only; remove an event alias.")
    @app_commands.autocomplete(event=autocompletes.event_id())
    @app_commands.describe(event="Event name or ID.", alias="Alias to remove.")
    async def event_remove(self, interaction: discord.Interaction, event: str, alias: str) -> None:
        await self._do_remove(interaction, "event", event, alias)

    # holomem
    @alias_holomem.command(name="list", description="Authorized only; view a holomem's aliases.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    @app_commands.describe(holomem="Holomem name or ID.")
    async def holomem_list(self, interaction: discord.Interaction, holomem: str) -> None:
        await self._do_list(interaction, "holomem", holomem)

    @alias_holomem.command(name="add", description="Authorized only; add a holomem alias.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    @app_commands.describe(holomem="Holomem name or ID.", alias="Alias to add.")
    async def holomem_add(self, interaction: discord.Interaction, holomem: str, alias: str) -> None:
        await self._do_add(interaction, "holomem", holomem, alias)

    @alias_holomem.command(name="remove", description="Authorized only; remove a holomem alias.")
    @app_commands.autocomplete(holomem=autocompletes.holomem())
    @app_commands.describe(holomem="Holomem name or ID.", alias="Alias to remove.")
    async def holomem_remove(
        self, interaction: discord.Interaction, holomem: str, alias: str
    ) -> None:
        await self._do_remove(interaction, "holomem", holomem, alias)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(AliasCog(bot))
