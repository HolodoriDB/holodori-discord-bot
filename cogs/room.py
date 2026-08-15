from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Union

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.views import HoloView

if TYPE_CHECKING:
    from main import HolodoriBot

# room-code channel renamer. a room channel is named "<name>-<code>" (e.g. g1-1111-2222); people
# share the game's room code by renaming the channel. the code is 8 digits shown as XXXX-YYYY, so a
# room channel looks like g1-1111-2222.

RoomChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.Thread]

PREFIX = "%rm"
PLACEHOLDER = "xxxx-xxxx"
RENAME_LIMIT = 2
RENAME_WINDOW = 600.0  # discord only allows a channel to be renamed twice per 10 minutes
_RM_COLOR = 0xE85D9E
_PROMPT_COLOR = 0x0099FF

WRONG_FORMAT = "Wrong channel format. Channel name needs to be in the format *-####-####"
WRONG_CODE_LENGTH = "Room code must be 8 characters long"
GENERIC_ERROR = "Error occured trying to change channel name."

# code = 4+4 digits (or x placeholders) joined by a dash; players = a single 0-4 (0 shown as "f")
_CODE = r"[0-9x]{4}-[0-9x]{4}"
_ROOM_RE = re.compile(rf"^(?P<name>.+)-(?P<code>{_CODE})(?:-(?P<players>[0-9fx]))?$")
_HAS_CODE = re.compile(rf"-{_CODE}(?:-[0-9fx])?$")

# a code may be typed with or without the dash: 1111-2222 or 11112222
_CODE_TOKEN = re.compile(r"^(\d{4})-?(\d{4})$")
_MSG_PLAYERS = re.compile(r"^\+([0-4])$")

_PLAYER_CHOICES = [
    app_commands.Choice(name="0 (Full)", value="f"),
    app_commands.Choice(name="1", value="1"),
    app_commands.Choice(name="2", value="2"),
    app_commands.Choice(name="3", value="3"),
    app_commands.Choice(name="4", value="4"),
]

# in-memory fallback for the rename ratelimit when no database is configured
_rename_log: dict[int, list[float]] = {}


def _room_channel(channel: object) -> RoomChannel | None:
    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
        return channel
    return None


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _fmt_code(code: int) -> str:
    s = f"{code:08d}"
    return f"{s[:4]}-{s[4:]}"


def _code_int(token: str) -> int:
    return int(token.replace("-", ""))


def _norm_players(raw: str | None) -> str | None:
    return "f" if raw == "0" else raw  # 0 open slots == full


def _is_room(name: str | None) -> bool:
    return bool(name and _HAS_CODE.search(name))


def _parse(name: str) -> tuple[str, str, str | None] | None:
    m = _ROOM_RE.match(name)
    return (m.group("name"), m.group("code"), m.group("players")) if m else None


def _new_name(
    current: str, *, code: int | None = None, players: str | None = None, close: bool = False
) -> str | None:
    parsed = _parse(current)
    if not parsed:
        return None
    base, cur_code, cur_players = parsed
    if close:
        return f"{base}-{PLACEHOLDER}"
    new_code = _fmt_code(code) if code is not None else cur_code
    new_players = players if players is not None else cur_players
    return f"{base}-{new_code}-{new_players}" if new_players else f"{base}-{new_code}"


class _RoomConfirm(HoloView):
    # confirmation prompt: anyone may answer, and the prompt is deleted once answered
    def __init__(self, cog: RoomCog, channel: RoomChannel, new_name: str) -> None:
        super().__init__(timeout=30)
        self.cog = cog
        self.channel = channel
        self.new_name = new_name

    async def _cleanup(self) -> None:
        if self.message:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass

    @discord.ui.button(label="YES", style=discord.ButtonStyle.secondary, emoji="✔️")
    async def yes(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer()
        kind, message = await self.cog.change_name(self.channel, self.new_name)
        await self.channel.send(embed=self.cog.result_embed(kind, message))
        await self._cleanup()
        self.stop()

    @discord.ui.button(label="NO", style=discord.ButtonStyle.secondary, emoji="❌")
    async def no(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self._cleanup()
        self.stop()

    async def on_timeout(self) -> None:
        await self._cleanup()


class RoomCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    # --- embeds (title + a type/message field) ---

    def result_embed(self, kind: str, message: str) -> discord.Embed:
        em = embeds.embed(title="Rm", color=discord.Color(_RM_COLOR))
        em.add_field(name=_cap(kind), value=_cap(message))
        if self.bot.user:
            em.set_thumbnail(url=self.bot.user.display_avatar.url)
        return em

    # --- rename ratelimit (persisted to psql so the 10-minute window survives restarts) ---

    async def _times(self, cid: int) -> list[float]:
        if self.bot.user_data:
            return await self.bot.user_data.get_room_renames(cid)
        return list(_rename_log.get(cid, []))

    async def _save_times(self, cid: int, times: list[float]) -> None:
        if self.bot.user_data:
            await self.bot.user_data.set_room_renames(cid, times)
        else:
            _rename_log[cid] = times

    async def change_name(self, channel: RoomChannel, new_name: str) -> tuple[str, str]:
        if new_name == channel.name:
            return "Success", f"Channel name changed to {new_name}"
        now = time.time()
        times = [t for t in await self._times(channel.id) if now - t < RENAME_WINDOW]
        if len(times) >= RENAME_LIMIT:
            nxt = int(times[0] + RENAME_WINDOW)
            return "error", (
                "You can only change the name of a channel twice every 10 minutes.\n"
                f"Next change <t:{nxt}:R>\nChannel Name: `{new_name}`"
            )
        try:
            await channel.edit(name=new_name)
        except discord.HTTPException:
            return "error", GENERIC_ERROR
        times.append(now)
        await self._save_times(channel.id, times[-RENAME_LIMIT:])
        return "Success", f"Channel name changed to {new_name}"

    # --- slash command ---

    rm = app_commands.Group(
        name="rm",
        description="Changes the channel name to the given code and players.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=False, private_channel=False
        ),
    )

    async def _do(
        self,
        interaction: discord.Interaction,
        *,
        code: int | None = None,
        players: str | None = None,
        close: bool = False,
    ) -> None:
        channel = _room_channel(interaction.channel)
        new = _new_name(channel.name, code=code, players=players, close=close) if channel else None
        if channel is None or new is None:
            await interaction.response.send_message(embed=self.result_embed("Error", WRONG_FORMAT))
            return
        kind, message = await self.change_name(channel, new)
        await interaction.response.send_message(embed=self.result_embed(kind, message))

    @rm.command(name="code", description="Change a room's code.")
    @app_commands.describe(code="The new room code, digits only (e.g. 11112222).")
    async def code(
        self, interaction: discord.Interaction, code: app_commands.Range[int, 0, 99999999]
    ) -> None:
        await self._do(interaction, code=code)

    @rm.command(name="players", description="Change a room's players.")
    @app_commands.choices(players=_PLAYER_CHOICES)
    async def players(
        self, interaction: discord.Interaction, players: app_commands.Choice[str]
    ) -> None:
        await self._do(interaction, players=players.value)

    @rm.command(name="both", description="Change a room's code and players.")
    @app_commands.describe(code="The new room code, digits only (e.g. 11112222).")
    @app_commands.choices(players=_PLAYER_CHOICES)
    async def both(
        self,
        interaction: discord.Interaction,
        code: app_commands.Range[int, 0, 99999999] | None = None,
        players: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._do(interaction, code=code, players=players.value if players else None)

    @rm.command(name="close", description="Close a room.")
    async def close(self, interaction: discord.Interaction) -> None:
        await self._do(interaction, close=True)

    # --- message commands ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        content = message.content.strip()
        if content.lower().split(" ", 1)[0] == PREFIX:  # %rm ... (immediate, like /rm)
            await self._execute_message(message, content)
            return
        # a bare code (1111-2222 / 11112222) or a +N players message -> confirm, then rename
        channel = _room_channel(message.channel)
        if channel is None or not _is_room(channel.name):
            return
        code: int | None = None
        players: str | None = None
        if m := _CODE_TOKEN.match(content):
            code = _code_int(m.group(0))
        elif m := _MSG_PLAYERS.match(content):
            players = _norm_players(m.group(1))
        else:
            return
        new = _new_name(channel.name, code=code, players=players)
        if not new or new == channel.name:
            return
        view = _RoomConfirm(self, channel, new)
        view.message = await message.channel.send(
            embed=embeds.embed(
                title="Room Change",
                description=f"Do you want to change the room code to {new}?",
                color=discord.Color(_PROMPT_COLOR),
            ),
            view=view,
        )

    async def _execute_message(self, message: discord.Message, content: str) -> None:
        args = content.split()[1:]
        code: int | None = None
        players: str | None = None
        for arg in args:
            if code is None and _CODE_TOKEN.match(arg):
                code = _code_int(arg)
            elif players is None and re.fullmatch(r"[0-4]", arg):
                players = _norm_players(arg)

        channel = _room_channel(message.channel)
        if channel is None or not _is_room(channel.name):
            await message.reply(embed=self.result_embed("Error", WRONG_FORMAT))
            return

        if code is not None or players is not None:
            new = _new_name(channel.name, code=code, players=players)
        elif args and "".join(args).isdigit():  # numeric junk that wasn't a valid code
            await message.channel.send(
                embed=self.result_embed("error", f"Invalid arguments: {message.content}")
            )
            return
        else:  # no args (or non-numeric) -> close the room
            new = _new_name(channel.name, close=True)

        if new is None:
            await message.reply(embed=self.result_embed("Error", WRONG_FORMAT))
            return
        kind, msg = await self.change_name(channel, new)
        await message.channel.send(embed=self.result_embed(kind, msg))


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(RoomCog(bot))
