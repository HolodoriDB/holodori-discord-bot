"""shared detail-embed builders, reused by the info commands and the guessing reveal buttons."""

from __future__ import annotations

import asyncio
import io
import re
from typing import TYPE_CHECKING

import discord

from helpers import embeds, imaging
from helpers.emojis import emojis
from services.holodori import HolodoriError, HolodoriNotFound

if TYPE_CHECKING:
    from data.models import CardDetail, EventInfo, HolomemGroup, HolomemMember, SongDetail
    from main import HolodoriBot

_ATTR_COLORS = {"Cute": 0xFF6FA5, "Pure": 0x5AC8E6, "Happy": 0xFFB13D}
_SKILL_LABELS = {"passive": "Passive", "active": "Active", "special": "Special"}
_HIGHLIGHT_RE = re.compile(r"\[/?highlight\]")
_OTHER_TAG_RE = re.compile(r"\[[^\]]+\]")
_DIFF_ORDER = ["easy", "normal", "hard", "expert"]


def clean_skill_text(text: str) -> str:
    text = _HIGHLIGHT_RE.sub("**", text)
    text = _OTHER_TAG_RE.sub("", text)
    return text.strip()


def _diff_type(difficulty_type: str) -> str:
    return difficulty_type.rsplit("_", 1)[-1].lower()


def _diff_key(difficulty_type: str) -> int:
    t = _diff_type(difficulty_type)
    return _DIFF_ORDER.index(t) if t in _DIFF_ORDER else 99


def _fmt_length(seconds: int | None) -> str:
    if not seconds:
        return "—"
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- builders ---


async def unsquished_bytes(bot: HolodoriBot, path: str | None, aspect: float) -> bytes | None:
    """fetch a squished cdn asset and unsquish it to its true aspect (png bytes), or None."""
    if not path or not bot.holo:
        return None
    try:
        raw = await bot.holo.fetch_bytes(bot.holo.image_url(path))
        return await asyncio.to_thread(imaging.unsquish, raw, aspect)
    except Exception:
        return None


async def _unsquished_file(
    bot: HolodoriBot, path: str | None, aspect: float, name: str
) -> discord.File | None:
    png = await unsquished_bytes(bot, path, aspect)
    return discord.File(io.BytesIO(png), name) if png else None


async def card_embed(bot: HolodoriBot, card: CardDetail) -> tuple[discord.Embed, list[discord.File]]:
    assert bot.holo
    color = _ATTR_COLORS.get(card.attributeName or "", 0xE85D9E)
    attr = emojis.attr(card.attributeName)
    stat = emojis.stat(card.mainStat)
    lines = [
        f"**Character:** {card.character}",
        f"**Rarity:** {emojis.rarity(card.rarity)}",
        f"**Attribute:** {(attr + ' ') if attr else ''}{card.attributeName or '—'}",
        f"**Main Stat:** {(stat + ' ') if stat else ''}{(card.mainStat or '—').title()}",
        f"**Group:** {card.group.name if card.group else '—'}",
    ]
    if card.baseTotals:
        lines.append(f"**Max Base Total:** {card.baseTotals[-1]:,}")
    embed = embeds.embed(title=card.name, description="\n".join(lines), color=discord.Color(color))
    for skill in card.skills:
        desc = clean_skill_text(skill.descriptions[-1]) if skill.descriptions else "—"
        label = _SKILL_LABELS.get(skill.type, skill.type.title())
        embed.add_field(name=f"{label} Skill", value=desc[:1024] or "—", inline=False)
    files: list[discord.File] = []
    art = await _unsquished_file(bot, card.image, imaging.ASPECT_FULL, "card.png")
    if art:
        files.append(art)
        embed.set_image(url="attachment://card.png")
    elif card.image:
        embed.set_image(url=bot.holo.image_url(card.image))
    embed.set_footer(text=f"ID: {card.id}")
    return embed, files


def song_embed(bot: HolodoriBot, detail: SongDetail) -> discord.Embed:
    assert bot.holo and bot.data
    by = ", ".join(
        sorted({n.strip() for n in (detail.composer, detail.arranger, detail.lyricist) if n and n != "-"})
    )
    lines: list[str] = []
    if detail.characterGroupDisplayName:
        lines.append(f"**Singers:** {detail.characterGroupDisplayName}")
    lines.append(f"**ID:** `{detail.id}`")
    if by:
        lines.append(f"**By:** {by}")
    if detail.startTime:
        lines.append(f"**Released:** <t:{int(detail.startTime / 1000)}:D>")
    lines.append(f"**Length:** {_fmt_length(detail.playingSeconds)}")
    if detail.obtain and detail.obtain.get("type"):
        lines.append(f"**Obtain:** {detail.obtain['type'].title()}")
    lines.append("")
    for d in sorted(detail.difficulties, key=lambda x: _diff_key(x.difficultyType)):
        tname = _diff_type(d.difficultyType).title()
        notes = f" `({d.fullComboNoteCount} notes)`" if d.fullComboNoteCount else ""
        lines.append(f"**{tname}:** Lvl {d.difficultyLevel}{notes}")
    embed = embeds.embed(title=detail.title, description="\n".join(filter(None, lines)).strip())
    summary = bot.data.get_song(detail.id)
    if summary and summary.jacket:
        embed.set_thumbnail(url=bot.holo.image_url(summary.jacket))
    return embed


def holomem_embed(
    bot: HolodoriBot, group: HolomemGroup, member: HolomemMember
) -> discord.Embed:
    assert bot.holo
    lines = [f"**Branch:** {group.name}"]
    if member.stickers:
        lines.append(f"**Membership Stickers:** {len(member.stickers)}")
    embed = embeds.embed(title=member.name, description="\n".join(lines))
    icon = bot.holo.image_url(member.icon)
    if icon:
        embed.set_thumbnail(url=icon)
    if member.stickers and member.stickers[0].image:
        embed.set_image(url=bot.holo.image_url(member.stickers[0].image))
    embed.set_footer(text=f"ID: {member.id}")
    return embed


def event_ended(ev: EventInfo) -> bool:
    # an event with no end date is ongoing, not ended (the api reports live=false for those)
    return bool(ev.endTime) and not ev.live


async def event_embed(bot: HolodoriBot, ev: EventInfo) -> tuple[discord.Embed, list[discord.File]]:
    assert bot.holo
    status = "⚫ Ended" if event_ended(ev) else "🟢 Live"
    ends = f"<t:{ev.endTime // 1000}:R>" if ev.endTime else "No end date"
    lines = [
        f"**Type:** {'Song Score' if ev.isSongScore else 'Marathon'}",
        f"**ID:** `{ev.eventId}`",
        f"**Status:** {status}",
        f"**Starts:** {f'<t:{ev.startTime // 1000}:R>' if ev.startTime else '—'}",
        f"**Ends:** {ends}",
    ]
    embed = embeds.embed(title=ev.name, description="\n".join(lines))
    files: list[discord.File] = []
    logo = await _unsquished_file(bot, ev.logo, imaging.ASPECT_LOGO, "logo.png")
    if logo:
        files.append(logo)
        embed.set_thumbnail(url="attachment://logo.png")
    banner = await _unsquished_file(bot, ev.banner, imaging.ASPECT_BANNER, "banner.png")
    if banner:
        files.append(banner)
        embed.set_image(url="attachment://banner.png")
    return embed, files


def find_member(
    groups: list[HolomemGroup], holomem_id: str
) -> tuple[HolomemGroup, HolomemMember] | None:
    for g in groups:
        for m in g.members:
            if m.id == holomem_id:
                return g, m
    return None


# --- async fetchers (used by buttons; kind-dispatched) ---


async def build_info_embed(
    bot: HolodoriBot, kind: str, target_id: str, region: str, lang: str
) -> tuple[discord.Embed, list[discord.File]]:
    assert bot.holo and bot.data
    if kind == "song":
        return song_embed(bot, await bot.holo.get_song(target_id, lang)), []
    if kind == "card":
        return await card_embed(bot, await bot.holo.get_card(target_id, lang))
    if kind == "holomem":
        found = find_member(bot.data.holomem_groups(), target_id)
        if not found:
            raise HolodoriNotFound(404, "holomem")
        return holomem_embed(bot, *found), []
    if kind == "event":
        events = await bot.data.events(region, lang)
        ev = next((e for e in events if e.eventId == target_id), None)
        if not ev:
            raise HolodoriNotFound(404, "event")
        return await event_embed(bot, ev)
    raise HolodoriError(400, f"unknown kind {kind}")
