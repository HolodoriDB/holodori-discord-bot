from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

# the badge tray is 5 slots (S=1, M=2, L=3); a badge sits at a 1-indexed start slot and empty slots
# are real gaps (e.g. MxMxx, SSSxx). we lay them on a fixed 5-slot canvas so positions/gaps are
# faithful. _SLOT_W = 1.8*H so an L (3 slots) exactly matches the emblem art's 27/5 aspect. the whole
# strip is ~9:1, so a single media item renders as a small band (not one huge image per badge).
_BADGE_H = 96
_SLOT_W = 173
_N_SLOTS = 5


def _slots(size: str | None) -> int:
    s = str(size or "")
    return 3 if s.endswith("_L") else 2 if s.endswith("_M") else 1


def _compose_badge_strip(items: list[tuple[int, int, bytes]]) -> bytes | None:
    # items: (start_slot 1-5, slot_span, image bytes). each badge is fit (aspect-preserving, centered)
    # into its slot box; uncovered slots stay transparent.
    canvas = Image.new("RGBA", (_SLOT_W * _N_SLOTS, _BADGE_H), (0, 0, 0, 0))
    drew = False
    for start, span, raw in items:
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception:
            continue
        box_w = _SLOT_W * span
        scale = min(box_w / im.width, _BADGE_H / im.height)
        nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
        x0 = _SLOT_W * (start - 1)  # slots are 1-indexed
        canvas.paste(im, (x0 + (box_w - nw) // 2, (_BADGE_H - nh) // 2), im)
        drew = True
    if not drew:
        return None
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()

from helpers import embeds
from helpers.autocompletes import REGION_CHOICES, REGION_LABELS
from helpers.emojis import emojis
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
        # the fan mark as a custom emoji right next to the name (holomem fan badge, or the official
        # verified mark) - falls back to no emoji if it isn't uploaded
        fan = p.get("fanMark") or {}
        mark = emojis.fanmark(fan.get("assetId")) if fan.get("assetId") else None
        head = f"## {f'{mark} ' if mark else ''}{discord.utils.escape_markdown(name)}"
        if sub:
            head += f"\n{sub}"  # normal size (not -#), so rank + region stand out
        message = str(info.get("message") or "").strip()
        if message:
            head += f"\n-# {discord.utils.escape_markdown(message)}"
        container.add_item(discord.ui.TextDisplay(head))

        # equipped titles (badges) as one small strip (a full-width media item per badge is huge)
        strip = await self._badge_strip(p.get("emblems") or [])
        if strip:
            files.append(discord.File(io.BytesIO(strip), "badges.png"))
            container.add_item(
                discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://badges.png"))
            )

        # the custom palette image underneath
        pal_item = await self._palette_item(p.get("palette"), pid, region, files)
        if pal_item:
            container.add_item(discord.ui.MediaGallery(pal_item))

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return view, files

    def _badge_url(self, emblem: dict) -> str | None:
        # badges span 5 slots (S=1, M=2, L=3), and the art's own aspect encodes that width - so
        # compositing at a fixed height keeps SLS / ML / SSSSS etc. proportional. only the L emblems
        # are stored squished-into-POT, so point those at their true-aspect _unsquished sibling.
        img = emblem.get("image")
        if not img:
            return None
        if str(emblem.get("size") or "").endswith("_L") or "img_emb_l_" in img:
            return self.bot.holo.unsquished_image_url(img)
        return self.bot.holo.image_url(img)

    async def _badge_strip(self, emblems: list) -> bytes | None:
        # fetch each equipped badge (public cdn) at its slot; composite onto the 5-slot tray
        assert self.bot.holo
        want: list[tuple[int, int, str]] = []  # (start_slot, span, url)
        for e in emblems or []:
            url = self._badge_url(e)
            if not url:
                continue
            pos = e.get("position")
            start = pos if isinstance(pos, int) and 1 <= pos <= _N_SLOTS else 1
            want.append((start, _slots(e.get("size")), url))
        if not want:
            return None
        items: list[tuple[int, int, bytes]] = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for start, span, url in want:
                    try:
                        async with s.get(url) as r:
                            if r.status == 200:
                                items.append((start, span, await r.read()))
                    except aiohttp.ClientError:
                        continue
        except aiohttp.ClientError:
            return None
        if not items:
            return None
        return await asyncio.to_thread(_compose_badge_strip, items)

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
