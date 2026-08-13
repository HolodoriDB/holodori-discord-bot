"""discord application (bot) emojis, resolved from data/emojis.json.

names map to a manual_assets webp; scripts/upload_emojis.py fetches those, converts to png/gif and
uploads them as app emojis, writing the id/mention map here.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

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
        self._map: dict[str, str] = {}  # name -> mention string

    def _load_file(self) -> None:
        if os.path.exists(EMOJIS_FILE):
            with open(EMOJIS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._map = {k: v["mention"] for k, v in raw.items()}

    async def load(self, bot: HolodoriBot) -> None:
        self._load_file()
        if self._map:
            return
        # fall back to whatever the application already has uploaded
        try:
            existing: list[discord.Emoji] = await bot.fetch_application_emojis()
            self._map = {e.name: str(e) for e in existing}
        except Exception:
            pass

    def get(self, name: str) -> str:
        return self._map.get(name, "")

    def rarity(self, n: int, animated: bool = False) -> str:
        star = self._map.get("rarity_star_animated" if animated else "rarity_star")
        if not star:
            star = self._map.get("rarity_star", "")
        return star * n if star else "★" * n

    def attr(self, name: str | None) -> str:
        return self._map.get(f"attr_{(name or '').lower()}", "")

    def stat(self, name: str | None) -> str:
        return self._map.get(f"stat_{(name or '').lower()}", "")


emojis = Emojis()
