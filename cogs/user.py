from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import LANGUAGES, REGIONS
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


class _SettingsView(HoloView):
    def __init__(self, cog: UserCog, user_id: int, settings: dict) -> None:
        super().__init__(timeout=180, restrict_to=user_id)
        self.cog = cog
        self.user_id = user_id
        self.settings = settings
        self._build()

    def _build(self) -> None:
        self.clear_items()
        lang = discord.ui.Select(
            placeholder="Default language",
            options=[
                discord.SelectOption(
                    label=_LANG_LABELS[code],
                    value=code,
                    default=code == self.settings["default_language"],
                )
                for code in LANGUAGES
            ],
        )
        lang.callback = self._on_lang  # type: ignore[assignment]
        region = discord.ui.Select(
            placeholder="Default event region",
            options=[
                discord.SelectOption(
                    label=_REGION_LABELS[code],
                    value=code,
                    default=code == self.settings["default_region"],
                )
                for code in REGIONS
            ],
        )
        region.callback = self._on_region  # type: ignore[assignment]
        difficulty = discord.ui.Select(
            placeholder="Default chart difficulty",
            options=[
                discord.SelectOption(
                    label=d.title(), value=d, default=d == self.settings["default_difficulty"]
                )
                for d in _DIFFS
            ],
        )
        difficulty.callback = self._on_diff  # type: ignore[assignment]
        mirror_on = self.settings["mirror_charts_by_default"]
        mirror = discord.ui.Button(
            label=f"Mirror Charts: {'On' if mirror_on else 'Off'}",
            style=discord.ButtonStyle.success if mirror_on else discord.ButtonStyle.secondary,
        )
        mirror.callback = self._on_mirror  # type: ignore[assignment]
        self.add_item(lang)
        self.add_item(region)
        self.add_item(difficulty)
        self.add_item(mirror)

    def _embed(self) -> discord.Embed:
        s = self.settings
        return embeds.embed(
            title="Your Settings",
            description=(
                f"**Language:** {_LANG_LABELS.get(s['default_language'], s['default_language'])}\n"
                f"**Event region:** {_REGION_LABELS.get(s['default_region'], s['default_region'])}\n"
                f"**Chart difficulty:** {s['default_difficulty'].title()}\n"
                f"**Mirror charts:** {'On' if s['mirror_charts_by_default'] else 'Off'}\n"
                f"**Timezone:** `{s['timezone']}` (change with `/user timezone`)"
            ),
        )

    async def _save(self, interaction: discord.Interaction, key: str, value) -> None:
        assert self.cog.bot.user_data
        self.settings = await self.cog.bot.user_data.change_settings(self.user_id, key, value)
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_lang(self, interaction: discord.Interaction) -> None:
        await self._save(interaction, "default_language", interaction.data["values"][0])  # type: ignore[index]

    async def _on_region(self, interaction: discord.Interaction) -> None:
        await self._save(interaction, "default_region", interaction.data["values"][0])  # type: ignore[index]

    async def _on_diff(self, interaction: discord.Interaction) -> None:
        await self._save(interaction, "default_difficulty", interaction.data["values"][0])  # type: ignore[index]

    async def _on_mirror(self, interaction: discord.Interaction) -> None:
        await self._save(
            interaction, "mirror_charts_by_default", not self.settings["mirror_charts_by_default"]
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
        await interaction.response.send_message(embed=view._embed(), view=view)
        view.message = await interaction.original_response()

    @user.command(name="timezone", description="Set your timezone (IANA name, e.g. America/New_York).")
    @app_commands.describe(timezone="IANA timezone name.")
    async def timezone(self, interaction: discord.Interaction, timezone: str) -> None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            await interaction.response.send_message(
                embed=embeds.error_embed(f"`{timezone}` isn't a valid IANA timezone."),
                ephemeral=True,
            )
            return
        assert self.bot.user_data
        await self.bot.user_data.change_settings(interaction.user.id, "timezone", timezone)
        await interaction.response.send_message(
            embed=embeds.success_embed(f"Timezone set to `{timezone}`."), ephemeral=True
        )


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(UserCog(bot))
