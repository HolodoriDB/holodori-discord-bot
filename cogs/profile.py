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
# the equipped-title tray is 5 uniform 86.4px slots (432x86.4, from the UserEmblemView prefab: cell
# pitch 86.4, no edge padding). each emblem is pivot-CENTERED over its slot span (S=1/M=2/L=3) at its
# own cell size (S 86.4x86.4, M 169.56x79.79, L 224.64x69.12 - RectTransformSizeChange._values), so
# M/L carry the game's own small in-span padding and empty slots stay clear. rendered 2x for crispness.
_SCALE = 2
_SLOT = 86.4 * _SCALE
_N_SLOTS = 5
_TRAY_H = round(86.4 * _SCALE)
# emblem cell (width, height, slot span) per size
_EMBLEM = {"S": (86.4, 86.4, 1), "M": (169.56, 79.79, 2), "L": (224.64, 69.12, 3)}


def _size_char(size: str | None) -> str:
    s = str(size or "")
    return "L" if s.endswith("_L") else "M" if s.endswith("_M") else "S"


def _compose_badge_strip(items: list[tuple[int, str, bytes]]) -> bytes | None:
    # items: (start_slot 1-5, size 'S'/'M'/'L', image bytes); emblem is unsquished so its w:h already
    # matches the cell, so resizing to the cell size keeps the aspect. centered over its slot span.
    canvas = Image.new("RGBA", (round(_SLOT * _N_SLOTS), _TRAY_H), (0, 0, 0, 0))
    drew = False
    for start, size, raw in items:
        w0, h0, span = _EMBLEM.get(size, _EMBLEM["S"])
        w, h = round(w0 * _SCALE), round(h0 * _SCALE)
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA").resize((w, h), Image.Resampling.LANCZOS)
        except Exception:
            continue
        x_center = ((start - 1) + span / 2) * _SLOT  # centre of the emblem's slot span
        canvas.paste(im, (round(x_center - w / 2), round((_TRAY_H - h) / 2)), im)
        drew = True
    if not drew:
        return None
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()

from helpers import embeds, text_commands
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
        view, files, err = await self.build_profile(pid, region)
        if err is not None or view is None:
            await interaction.followup.send(embed=err or embeds.error_embed("Couldn't load that profile."))
            return
        await interaction.followup.send(view=view, files=files)

    @commands.command(name="profile")
    async def p_profile(self, ctx: commands.Context, *args: str) -> None:
        # %profile [region] {tier} - the profile of the player CURRENTLY at that rank (1-100)
        region, tier, leftover = text_commands.parse_region_tier(list(args))
        if tier is None or leftover:
            await ctx.reply(
                embed=text_commands.help_embed(
                    "profile", "[region] {tier}", any_order=True, aliases=[]
                ),
                mention_author=False,
            )
            return
        assert self.bot.holo and self.bot.user_data
        region = await self._region(ctx.author.id, region or "default")
        async with ctx.typing():
            if tier > 100:
                await ctx.reply(
                    embed=embeds.error_embed(
                        f"T{tier} is a cutoff line, not a player - use a rank from 1-100."
                    ),
                    mention_author=False,
                )
                return
            # the authorized leaderboard keeps each row's stable userId (= the public friend code
            # the profile fetch keys on); find the one currently sitting at this rank
            try:
                lb = await self.bot.holo.get_event_leaderboard_ids(region)
            except HolodoriError as e:
                await ctx.reply(
                    embed=embeds.error_embed(f"Couldn't fetch the leaderboard: {e.detail or e.status}"),
                    mention_author=False,
                )
                return
            pid = next(
                (
                    str(r["userId"])
                    for r in (lb.get("rankings") or [])
                    if r.get("rank") == tier and r.get("userId")
                ),
                None,
            )
            if not pid:
                await ctx.reply(
                    embed=embeds.error_embed(
                        f"No tracked player is at T{tier} on {REGION_LABELS.get(region, region)}."
                    ),
                    mention_author=False,
                )
                return
            view, files, err = await self.build_profile(pid, region)
        if err is not None or view is None:
            await ctx.reply(
                embed=err or embeds.error_embed("Couldn't load that profile."),
                mention_author=False,
            )
            return
        await ctx.reply(view=view, files=files, mention_author=False)

    async def build_profile(
        self, pid: str, region: str
    ) -> tuple["discord.ui.LayoutView | None", list[discord.File], "discord.Embed | None"]:
        # shared by /holodori profile and the %player "Profile" button: (view, files, None) or
        # (None, [], error_embed). the caller sends it.
        assert self.bot.holo
        await self.bot.holo.ensure_asset_info()  # the public cdn base, for the badge/palette urls
        try:
            data = await self.bot.holo.get_profile(pid, region)
        except HolodoriNotFound:
            # a "private" profile still fetches by id (private only hides the id in-game, which we
            # don't show anyway) - an empty reply means the id itself doesn't exist
            return None, [], embeds.error_embed(
                f"No player found with the ID `{pid}` on {REGION_LABELS.get(region, region)}."
            )
        except HolodoriError as e:
            return None, [], embeds.error_embed(
                f"Couldn't fetch that profile: {e.detail or e.status}"
            )
        view, files = await self._build_card(data, pid, region)
        return view, files, None

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
        head = f"## {discord.utils.escape_markdown(name)}{f' {mark}' if mark else ''}"
        if sub:
            head += f"\n{sub}"  # normal size (not -#), so rank + region stand out
        message = str(info.get("message") or "").strip()
        if message:
            # the bio as a blockquote, escaped + quoted on every line (bios can be multi-line)
            bio = discord.utils.escape_markdown(message).replace("\n", "\n> ")
            head += f"\n> {bio}"
        container.add_item(discord.ui.TextDisplay(head))

        # our own detected affiliations - just their descriptions (bot always reads the "en" key).
        # NOT escaped: these are our curated strings (the OSHI one carries a link we want clickable)
        descs = [
            d for a in (p.get("affiliations") or []) if (d := (a.get("description") or {}).get("en"))
        ]
        if descs:
            container.add_item(discord.ui.TextDisplay("\n".join(f"- {d}" for d in descs)))

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
        size = str(emblem.get("size") or "")
        # M and L emblems are stored squished-into-POT (extract._unsquish_aspect); S is 1:1 natural
        if size.endswith(("_M", "_L")) or "img_emb_m_" in img or "img_emb_l_" in img:
            return self.bot.holo.unsquished_image_url(img)
        return self.bot.holo.image_url(img)

    async def _badge_strip(self, emblems: list) -> bytes | None:
        # fetch each equipped badge (public cdn) at its slot; composite onto the 5-slot tray
        assert self.bot.holo
        want: list[tuple[int, str, str]] = []  # (start_slot, size 'S'/'M'/'L', url)
        for e in emblems or []:
            url = self._badge_url(e)
            if not url:
                continue
            pos = e.get("position")
            start = pos if isinstance(pos, int) and 1 <= pos <= _N_SLOTS else 1
            want.append((start, _size_char(e.get("size")), url))
        if not want:
            return None
        items: list[tuple[int, str, bytes]] = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for start, size, url in want:
                    try:
                        async with s.get(url) as r:
                            if r.status == 200:
                                items.append((start, size, await r.read()))
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
