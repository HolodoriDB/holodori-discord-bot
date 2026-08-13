from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds, timezones
from helpers.autocompletes import REGIONS
from helpers.views import HoloView

if TYPE_CHECKING:
    from main import HolodoriBot

_LANG_LABELS = {
    "eng": "English",
    "jpn": "日本語",
    "kor": "한국어",
    "cht": "繁體中文",
    "chs": "简体中文",
    "ind": "Indonesia",
}
_REGION_LABELS = {"us": "US", "as": "Asia", "jp": "Japan"}
_DIFFS = ["easy", "normal", "hard", "expert"]

# key -> (display name, kind, options[(label, value)] | None, explanation)
_SETTINGS: dict[str, tuple[str, str, list[tuple[str, str]] | None, str]] = {
    "default_language": (
        "Default Language",
        "select",
        [(v, k) for k, v in _LANG_LABELS.items()],
        "The language cards, songs, and events are shown in.",
    ),
    "default_region": (
        "Default Region",
        "select",
        [(_REGION_LABELS[r], r) for r in REGIONS],
        "The default game server for events and leaderboards.",
    ),
    "default_difficulty": (
        "Default Difficulty",
        "select",
        [(d.title(), d) for d in _DIFFS],
        "The default chart difficulty for `/song chart`.",
    ),
    "mirror_charts_by_default": (
        "Mirror Charts",
        "toggle",
        None,
        "Whether `/song chart` mirrors charts by default.",
    ),
}


def _label(key: str, value: Any) -> str:
    if key == "default_language":
        return _LANG_LABELS.get(str(value), str(value))
    if key == "default_region":
        return _REGION_LABELS.get(str(value), str(value))
    if key == "default_difficulty":
        return str(value).title()
    if key == "mirror_charts_by_default":
        return "On" if value else "Off"
    return str(value)


class _SettingsView(HoloView):
    """two-tier settings: pick a setting, then edit it on its own screen."""

    def __init__(self, cog: UserCog, user_id: int, settings: dict) -> None:
        super().__init__(timeout=180, restrict_to=user_id)
        self.cog = cog
        self.user_id = user_id
        self.settings = settings
        self._show_pick()

    def landing_embed(self) -> discord.Embed:
        return embeds.embed(
            title="Changing Settings", description="Select the setting you'd like to change."
        )

    def _setting_embed(self, key: str) -> discord.Embed:
        name, _kind, _opts, explain = _SETTINGS[key]
        return embeds.embed(
            title=f"{name} Setting",
            description=f"Currently set to `{_label(key, self.settings[key])}`.\n\n-# {explain}",
        )

    def _show_pick(self) -> None:
        self.clear_items()
        pick = discord.ui.Select(
            placeholder="Select a setting...",
            options=[discord.SelectOption(label=meta[0], value=key) for key, meta in _SETTINGS.items()],
        )
        pick.callback = self._on_pick  # type: ignore[method-assign]
        self.add_item(pick)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await self._open(interaction, interaction.data["values"][0])  # type: ignore[index]

    async def _open(self, interaction: discord.Interaction, key: str) -> None:
        _name, kind, opts, _explain = _SETTINGS[key]
        self.clear_items()
        if kind == "toggle":
            current = bool(self.settings[key])
            btn = discord.ui.Button(
                label=f"Change to {not current}",
                style=discord.ButtonStyle.danger if current else discord.ButtonStyle.success,
            )
            btn.callback = self._toggle_cb(key)  # type: ignore[method-assign]
            self.add_item(btn)
        else:
            assert opts is not None
            current_val = self.settings[key]
            sel = discord.ui.Select(
                placeholder="Choose a value...",
                options=[
                    discord.SelectOption(label=lbl, value=val, default=val == current_val)
                    for lbl, val in opts
                ],
            )
            sel.callback = self._value_cb(key)  # type: ignore[method-assign]
            self.add_item(sel)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back  # type: ignore[method-assign]
        self.add_item(back)
        await interaction.response.edit_message(embed=self._setting_embed(key), view=self)

    def _value_cb(self, key: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def cb(interaction: discord.Interaction) -> None:
            await self._change(interaction, key, interaction.data["values"][0])  # type: ignore[index]

        return cb

    def _toggle_cb(self, key: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def cb(interaction: discord.Interaction) -> None:
            await self._change(interaction, key, not self.settings[key])

        return cb

    async def _back(self, interaction: discord.Interaction) -> None:
        self._show_pick()
        await interaction.response.edit_message(embed=self.landing_embed(), view=self)

    async def _change(self, interaction: discord.Interaction, key: str, value: Any) -> None:
        assert self.cog.bot.user_data
        self.settings = await self.cog.bot.user_data.change_settings(self.user_id, key, value)
        await self._open(interaction, key)
        await interaction.followup.send(
            embed=embeds.success_embed("Setting changed."), ephemeral=True
        )


class UserCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    user = app_commands.Group(
        name="user",
        description="Your bot settings.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @user.command(name="settings", description="View and change your settings.")
    async def settings(self, interaction: discord.Interaction) -> None:
        assert self.bot.user_data
        current = await self.bot.user_data.get_settings(interaction.user.id)
        view = _SettingsView(self, interaction.user.id, current)
        await interaction.response.send_message(embed=view.landing_embed(), view=view)
        view.message = await interaction.original_response()

    @user.command(name="timezone", description="View or set your timezone.")
    @app_commands.describe(timezone="Common alias (ET, JST, ...) or IANA name; omit to view current.")
    async def timezone(
        self, interaction: discord.Interaction, timezone: str | None = None
    ) -> None:
        assert self.bot.user_data
        current = await self.bot.user_data.get_settings(interaction.user.id, "timezone")
        if timezone is None:
            await interaction.response.send_message(
                embed=embeds.embed(
                    title="Timezone",
                    description=f"Your timezone is `{current}`. Pass one to change it.",
                ),
                ephemeral=True,
            )
            return
        canon = timezones.canonical(timezone)
        if not canon:
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"`{timezone}` isn't a valid timezone. Use a common one "
                    f"({timezones.COMMON}) or an IANA name like `Europe/Paris`."
                ),
                ephemeral=True,
            )
            return
        await self.bot.user_data.change_settings(interaction.user.id, "timezone", canon)
        await interaction.response.send_message(
            embed=embeds.success_embed(f"Timezone set to `{canon}`."), ephemeral=True
        )


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(UserCog(bot))
