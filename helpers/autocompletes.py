from __future__ import annotations

import time
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands

if TYPE_CHECKING:
    from data.holodori import HolodoriData

AutocompleteCb = Callable[
    [discord.Interaction, str], Awaitable[list[app_commands.Choice[str]]]
]
AutocompleteIntCb = Callable[
    [discord.Interaction, str], Awaitable[list[app_commands.Choice[int]]]
]

LANGUAGES = ["eng", "jpn", "kor", "cht", "chs", "ind"]
REGIONS = ["us", "as", "jp"]
REGION_LABELS = {"us": "US", "as": "Asia", "jp": "Japan"}
REGION_ABBR = {"us": "US", "as": "AS", "jp": "JP"}
# includes a Default option that resolves to the user's default_region setting
REGION_CHOICES = [app_commands.Choice(name="Default", value="default")] + [
    app_commands.Choice(name=REGION_LABELS[r], value=r) for r in REGIONS
]


class Autocompletes:
    def __init__(self) -> None:
        self.holodori: HolodoriData | None = None
        self._tiers: list[tuple[int, str]] = []  # (rank, "US/AS/JP")
        self._tiers_at = 0.0

    async def _tier_list(self) -> list[tuple[int, str]]:
        # border ranks change rarely, so cache the cross-region set for a while
        now = time.time()
        if self._tiers and now - self._tiers_at < 600:
            return self._tiers
        if not self.holodori:
            return self._tiers
        try:
            data = await self.holodori.client.get_event_tiers()
        except Exception:
            return self._tiers
        tiers: list[tuple[int, str]] = []
        for t in data:
            if t.get("rank") is None:
                continue
            regs = [str(r) for r in (t.get("regions") or [])]
            # only tag regions when the border is missing from one or more; blank if in all three
            label = "" if len(regs) >= len(REGIONS) else "/".join(REGION_ABBR.get(r, r.upper()) for r in regs)
            tiers.append((int(t["rank"]), label))
        self._tiers = tiers
        self._tiers_at = now
        return self._tiers

    def tier(self) -> AutocompleteIntCb:
        async def cb(interaction: discord.Interaction, current: str):
            q = current.strip()
            # ranks 1-100 are individual players (all regions, no tag); borders are the cutoffs
            opts = [(r, "") for r in range(100, 0, -1)] + await self._tier_list()
            out: list[app_commands.Choice[int]] = []
            for rank, label in opts:
                if q and not str(rank).startswith(q):
                    continue
                out.append(
                    app_commands.Choice(name=f"{rank} ({label})" if label else str(rank), value=rank)
                )
                if len(out) >= 25:
                    break
            return out

        return cb

    def card(self) -> AutocompleteCb:
        async def cb(interaction: discord.Interaction, current: str):
            if not self.holodori:
                return []
            return [
                app_commands.Choice(name=f"{c.name} · {c.character}"[:100], value=c.id)
                for c in self.holodori.search_cards(current, 25)
            ]

        return cb

    def song(self) -> AutocompleteCb:
        async def cb(interaction: discord.Interaction, current: str):
            if not self.holodori:
                return []
            return [
                app_commands.Choice(name=s.title[:100], value=s.id)
                for s in self.holodori.search_songs_aliased(current, 25)
            ]

        return cb

    def holomem(self) -> AutocompleteCb:
        async def cb(interaction: discord.Interaction, current: str):
            if not self.holodori:
                return []
            return [
                app_commands.Choice(name=h.name[:100], value=h.id)
                for h in self.holodori.search_holomems_aliased(current, 25)
            ]

        return cb

    def event(self) -> AutocompleteCb:
        async def cb(interaction: discord.Interaction, current: str):
            if not self.holodori:
                return []
            region = getattr(interaction.namespace, "region", None) or REGIONS[0]
            try:
                events = await self.holodori.events(region, self.holodori.client.lang)
            except Exception:
                return []
            return [
                app_commands.Choice(name=e.name[:100], value=e.eventId)
                for e in self.holodori.search_events_aliased(events, current, 25)
            ]

        return cb

    def event_id(self) -> AutocompleteCb:
        # events have no real name (only a generic label), so alias management picks + displays them
        # by id; matching is alias-aware so an existing alias also surfaces the event
        async def cb(interaction: discord.Interaction, current: str):
            if not self.holodori:
                return []
            region = getattr(interaction.namespace, "region", None) or REGIONS[0]
            try:
                events = await self.holodori.events(region, self.holodori.client.lang)
            except Exception:
                return []
            return [
                app_commands.Choice(name=e.eventId[:100], value=e.eventId)
                for e in self.holodori.search_events_aliased(events, current, 25)
            ]

        return cb

    def choices(self, values: list[str]) -> AutocompleteCb:
        async def cb(interaction: discord.Interaction, current: str):
            q = current.lower().strip()
            return [
                app_commands.Choice(name=v, value=v)
                for v in values
                if q in v.lower()
            ][:25]

        return cb


autocompletes = Autocompletes()
