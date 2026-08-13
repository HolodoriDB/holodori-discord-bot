from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from discord import app_commands

if TYPE_CHECKING:
    from data.holodori import HolodoriData

AutocompleteCb = Callable[
    [discord.Interaction, str], Awaitable[list[app_commands.Choice[str]]]
]

LANGUAGES = ["eng", "jpn", "kor", "cht", "chs", "ind"]
REGIONS = ["us", "as", "jp"]


class Autocompletes:
    def __init__(self) -> None:
        self.holodori: HolodoriData | None = None

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
                for s in self.holodori.search_songs(current, 25)
            ]

        return cb

    def holomem(self) -> AutocompleteCb:
        async def cb(interaction: discord.Interaction, current: str):
            if not self.holodori:
                return []
            return [
                app_commands.Choice(name=h.name[:100], value=h.id)
                for h in self.holodori.search_holomems(current, 25)
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
            q = current.lower().strip()
            hits = [e for e in events if not q or q in e.name.lower() or q in e.eventId.lower()]
            return [
                app_commands.Choice(name=e.name[:100], value=e.eventId) for e in hits[:25]
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
