from __future__ import annotations

import io
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS
from services.holodori import HolodoriError, HolodoriNotFound

if TYPE_CHECKING:
    from main import HolodoriBot

_ACCENT = 0x8B5CF6


class ProfileCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    holodori = app_commands.Group(
        name="holodori",
        description="hololive Dreams account tools.",
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

    @holodori.command(name="profile", description="View a player's profile by their in-game ID.")
    @app_commands.describe(
        id="The player's public ID (the friend code shown in-game).",
        region="Game server region.",
    )
    @app_commands.choices(region=REGION_CHOICES)
    async def profile(
        self, interaction: discord.Interaction, id: str, region: str = "default"
    ) -> None:
        assert self.bot.holo and self.bot.user_data
        await interaction.response.defer(thinking=True)
        region = await self._region(interaction.user.id, region)
        pid = id.strip().upper()
        if not pid:
            await interaction.followup.send(embed=embeds.error_embed("Give a player ID to look up."))
            return
        try:
            data = await self.bot.holo.get_profile(pid, region)
        except HolodoriNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "Profile not found, or the player has it set to private."
                )
            )
            return
        except HolodoriError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't fetch that profile: {e.detail or e.status}")
            )
            return
        view, files = await self._build_card(data, pid, region)
        await interaction.followup.send(view=view, files=files)

    async def _build_card(
        self, data: dict, pid: str, region: str
    ) -> tuple[discord.ui.LayoutView, list[discord.File]]:
        # a components-v2 card: username (+ level/bio) with the fan mark as the header thumbnail, the
        # equipped titles in one image row, and the profile's custom-palette image underneath
        assert self.bot.holo
        p = data.get("profile") or {}
        info = p.get("userProfileInfo") or {}
        name = str(info.get("name") or "Unknown")
        files: list[discord.File] = []
        container = discord.ui.Container(accent_colour=discord.Colour(_ACCENT))

        sub = " · ".join(
            x
            for x in (
                f"Dream Rank {info['level']}" if info.get("level") else "",
                REGION_LABELS.get(region, region),
            )
            if x
        )
        head = f"## {discord.utils.escape_markdown(name)}"
        if sub:
            head += f"\n{sub}"  # normal size (not -#), so rank + region stand out
        message = str(info.get("message") or "").strip()
        if message:
            head += f"\n-# {discord.utils.escape_markdown(message)}"
        header = discord.ui.TextDisplay(head)

        fan = p.get("fanMark") or {}
        fan_url = self.bot.holo.image_url(fan.get("image")) if fan.get("image") else None
        if fan_url:
            container.add_item(discord.ui.Section(header, accessory=discord.ui.Thumbnail(fan_url)))
        else:
            container.add_item(header)

        # equipped titles (badges), one row of images
        badges = [
            u
            for e in (p.get("emblems") or [])[:10]
            if e.get("image") and (u := self.bot.holo.image_url(e["image"]))
        ]
        if badges:
            container.add_item(
                discord.ui.MediaGallery(*[discord.MediaGalleryItem(u) for u in badges])
            )

        # the custom palette image underneath
        pal_item = await self._palette_item(p.get("palette"), pid, region, files)
        if pal_item:
            container.add_item(discord.ui.MediaGallery(pal_item))

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return view, files

    async def _palette_item(
        self, palette: dict | None, pid: str, region: str, files: list[discord.File]
    ) -> "discord.MediaGalleryItem | None":
        assert self.bot.holo
        if not palette:
            return None
        if palette.get("custom"):
            # gated cloud-cdn jpg: fetch the proxied bytes and attach them (discord can't reach it)
            try:
                raw = await self.bot.holo.get_profile_palette(pid, region)
            except HolodoriError:
                return None
            files.append(discord.File(io.BytesIO(raw), "palette.jpg"))
            return discord.MediaGalleryItem("attachment://palette.jpg")
        if palette.get("image"):  # a card / holomem-preset background (squished-POT -> unsquished)
            url = self.bot.holo.unsquished_image_url(palette["image"])
            if url:
                return discord.MediaGalleryItem(url)
        return None


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(ProfileCog(bot))
