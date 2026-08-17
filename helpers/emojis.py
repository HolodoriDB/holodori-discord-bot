"""discord application (bot) emojis, resolved from data/emojis.json.

names map to a manual_assets webp (or, for fanmarks, a cdn asset); scripts/upload_emojis.py uploads
them as app emojis and writes the id/mention map here. text usages get a mention string; button
emojis get a PartialEmoji.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from main import HolodoriBot

EMOJIS_FILE = "data/emojis.json"

# emoji name -> manual_assets path
DEFS: dict[str, str] = {
    "rarity_star": "rarity-star.webp",
    "rarity_star_animated": "rarity-star-animated.webp",
    "attr_cute": "attr-cute.webp",
    "attr_pure": "attr-pure.webp",
    "attr_happy": "attr-happy.webp",
    "stat_performance": "stat-performance.webp",
    "stat_sense": "stat-sense.webp",
    "stat_technique": "stat-technique.webp",
    "bloom": "bloom-flower.webp",
}


class Emojis:
    def __init__(self) -> None:
        self._map: dict[str, str] = {}  # name -> mention string (text)
        self._emojis: dict[str, discord.PartialEmoji] = {}  # name -> partial (button emoji)

    def _add(self, name: str, emoji_id: int, animated: bool, mention: str | None = None) -> None:
        partial = discord.PartialEmoji(name=name, id=emoji_id, animated=animated)
        self._emojis[name] = partial
        self._map[name] = mention or str(partial)

    def _load_file(self) -> None:
        if not os.path.exists(EMOJIS_FILE):
            return
        with open(EMOJIS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for name, rec in raw.items():
            try:
                self._add(name, int(rec["id"]), bool(rec.get("animated")), rec.get("mention"))
            except (KeyError, ValueError, TypeError):
                continue

    async def load(self, bot: HolodoriBot) -> None:
        self._load_file()
        if self._map:
            return
        # fall back to whatever the application already has uploaded
        try:
            for e in await bot.fetch_application_emojis():
                self._add(e.name, e.id, e.animated, str(e))
        except Exception:
            pass

    def get(self, name: str) -> str:
        return self._map.get(name, "")

    def partial(self, name: str) -> discord.PartialEmoji | None:
        return self._emojis.get(name)

    def rarity(self, n: int, animated: bool = False) -> str:
        star = self._map.get("rarity_star_animated" if animated else "rarity_star")
        if not star:
            star = self._map.get("rarity_star", "")
        return star * n if star else "★" * n

    def fanmark(self, character_id: str | None) -> discord.PartialEmoji | None:
        # holomem fanmark button emoji, keyed by character id (chr-04001 -> chr_04001_fanmark)
        if not character_id:
            return None
        return self._emojis.get(f"chr_{character_id.split('-')[-1]}_fanmark")

    def attr(self, name: str | None) -> str:
        return self._map.get(f"attr_{(name or '').lower()}", "")

    def stat(self, name: str | None) -> str:
        return self._map.get(f"stat_{(name or '').lower()}", "")


emojis = Emojis()
