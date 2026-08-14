"""in-memory holodori game-data store: version-gated refresh, disk cache, plain-substring search.

mirrors the architecture of a per-region master-data cache but simpler: the api already returns
digested json, and there is a single `asset_hashes.revision` we gate refetches on. search is plain
case-insensitive substring ranking (no romanization/fuzzy matching).
"""

import asyncio
import json
import os
import time

from data import search
from data.models import Card, EventInfo, Holomem, HolomemGroup, Song
from helpers.logging import LOGGING
from services.holodori import HolodoriClient

CACHE_DIR = "cache"


class HolodoriData:
    def __init__(self, client: HolodoriClient, *, refresh_interval: int = 300) -> None:
        self.client = client
        self.refresh_interval = refresh_interval
        self._revision = -1
        self._cards: list[Card] = []
        self._cards_by_id: dict[str, Card] = {}
        self._songs: list[Song] = []
        self._songs_by_id: dict[str, Song] = {}
        self._holomems: list[Holomem] = []
        self._holomems_by_id: dict[str, Holomem] = {}
        self._groups: list[HolomemGroup] = []
        self._items: list[dict] = []
        self._events_cache: dict[tuple[str, str], tuple[float, list[EventInfo]]] = {}
        self._task: asyncio.Task | None = None

    # --- lifecycle ---

    async def start(self) -> None:
        self._load_from_disk()
        try:
            await self.refresh()
        except Exception as e:
            LOGGING.warnprint(f"holodori initial refresh failed: {e}")
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_interval)
            try:
                await self.refresh()
            except Exception as e:
                LOGGING.warnprint(f"holodori refresh failed: {e}")

    async def refresh(self) -> None:
        info = await self.client.get_asset_info()
        if info.revision == self._revision and self._cards:
            return  # nothing changed
        cards = await self.client.get_cards()
        songs = await self.client.get_songs()
        holomems = await self.client.get_holomems()
        groups = await self.client.get_holomem_groups()
        try:
            items = await self.client.get_items()
        except Exception:
            items = self._items
        self._apply(cards, songs, holomems, groups, items)
        self._revision = info.revision
        self._persist()
        LOGGING.infoprint(
            f"holodori data refreshed (rev {info.revision}): "
            f"{len(cards)} cards, {len(songs)} songs, {len(holomems)} holomems"
        )

    def _apply(
        self,
        cards: list[Card],
        songs: list[Song],
        holomems: list[Holomem],
        groups: list[HolomemGroup],
        items: list[dict],
    ) -> None:
        self._cards = sorted(cards, key=lambda c: c.order)
        self._cards_by_id = {c.id: c for c in cards}
        # m9999 is a placeholder song, never user-facing - drop it everywhere (search, matching, lists)
        songs = [s for s in songs if s.id != "m9999"]
        self._songs = sorted(songs, key=lambda s: (s.startTime or 0))
        self._songs_by_id = {s.id: s for s in songs}
        self._holomems = sorted(holomems, key=lambda h: h.order)
        self._holomems_by_id = {h.id: h for h in holomems}
        self._groups = groups
        self._items = items

    # --- disk cache (offline boot) ---

    def _path(self, name: str) -> str:
        return os.path.join(CACHE_DIR, name)

    def _persist(self) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        blob = {
            "revision": self._revision,
            "cards": [c.model_dump() for c in self._cards],
            "songs": [s.model_dump() for s in self._songs],
            "holomems": [h.model_dump() for h in self._holomems],
            "groups": [g.model_dump() for g in self._groups],
            "items": self._items,
        }
        try:
            with open(self._path("holodori_data.json"), "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False)
        except OSError as e:
            LOGGING.warnprint(f"holodori cache write failed: {e}")

    def _load_from_disk(self) -> None:
        path = self._path("holodori_data.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            self._apply(
                [Card.model_validate(c) for c in blob.get("cards", [])],
                [Song.model_validate(s) for s in blob.get("songs", [])],
                [Holomem.model_validate(h) for h in blob.get("holomems", [])],
                [HolomemGroup.model_validate(g) for g in blob.get("groups", [])],
                blob.get("items", []),
            )
            self._revision = blob.get("revision", -1)
        except Exception as e:
            LOGGING.warnprint(f"holodori cache load failed: {e}")

    # --- accessors ---

    def cards(self) -> list[Card]:
        return self._cards

    def get_card(self, card_id: str) -> Card | None:
        return self._cards_by_id.get(card_id)

    def songs(self) -> list[Song]:
        return self._songs

    def get_song(self, music_id: str) -> Song | None:
        return self._songs_by_id.get(music_id)

    def holomems(self) -> list[Holomem]:
        return self._holomems

    def get_holomem(self, holomem_id: str) -> Holomem | None:
        return self._holomems_by_id.get(holomem_id)

    def holomem_groups(self) -> list[HolomemGroup]:
        return self._groups

    def items(self) -> list[dict]:
        return self._items

    async def events(self, region: str, language: str, *, ttl: float = 60.0) -> list[EventInfo]:
        key = (region, language)
        now = time.monotonic()
        cached = self._events_cache.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
        evs = await self.client.get_events(region, language)
        self._events_cache[key] = (now, evs)
        return evs

    # --- fuzzy search (autocomplete) + single-answer resolution ---

    def search_cards(self, query: str, limit: int = 25) -> list[Card]:
        return search.rank(query, self._cards, lambda c: (c.name, c.character, c.id), limit)

    def search_songs(self, query: str, limit: int = 25) -> list[Song]:
        return search.rank(query, self._songs, lambda s: (s.title, s.id), limit)

    def search_holomems(self, query: str, limit: int = 25) -> list[Holomem]:
        return search.rank(query, self._holomems, lambda h: (h.name, h.shortName, h.id), limit)

    def match_song(self, query: str) -> Song | None:
        return search.best_match(query, self._songs, lambda s: (s.title, s.id))

    def match_card(self, query: str) -> Card | None:
        return search.best_match(query, self._cards, lambda c: (c.name, c.id))

    def match_holomem(self, query: str) -> Holomem | None:
        return search.best_match(query, self._holomems, lambda h: (h.name, h.shortName, h.id))
