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

# marathon normal (1) + relay (2): the only event types the bot tracks (leaderboards / graphs /
# score bonus), matching the backend's _MARATHON_EVENT_TYPES. every other archived event type
# (login campaigns, story, etc.) is filtered out so it can't appear as a valid event parameter.
TRACKED_EVENT_TYPES = frozenset({1, 2})

# manually-added search aliases, mirrored from the backend into a local copy. they poll on their own
# short loop (independent of the game-data revision) so an alias added via the bot in another process
# shows up quickly. NOTE: nothing matches against these yet - this is just the copy.
ALIAS_KINDS = ("song", "event", "holomem")
ALIAS_CACHE = "aliases.json"
ALIAS_REFRESH_INTERVAL = 120


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
        # kind -> {target_id: sorted alias list}; the local mirror of the backend alias store
        self._aliases: dict[str, dict[str, list[str]]] = {}
        self._task: asyncio.Task | None = None
        self._alias_task: asyncio.Task | None = None

    # --- lifecycle ---

    async def start(self) -> None:
        self._load_from_disk()
        self._load_aliases()
        try:
            await self.refresh()
        except Exception as e:
            LOGGING.warnprint(f"holodori initial refresh failed: {e}")
        try:
            await self.refresh_aliases()
        except Exception as e:
            LOGGING.warnprint(f"holodori initial alias refresh failed: {e}")
        self._task = asyncio.create_task(self._poll())
        self._alias_task = asyncio.create_task(self._poll_aliases())

    async def stop(self) -> None:
        for task in (self._task, self._alias_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._alias_task = None

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

    # --- aliases (local mirror of the backend store; not wired into any matching yet) ---

    def song_aliases(self, target_id: str) -> list[str]:
        return list(self._aliases.get("song", {}).get(target_id, ()))

    def event_aliases(self, target_id: str) -> list[str]:
        return list(self._aliases.get("event", {}).get(target_id, ()))

    def holomem_aliases(self, target_id: str) -> list[str]:
        return list(self._aliases.get("holomem", {}).get(target_id, ()))

    async def _fetch_aliases_for(self, kind: str) -> bool:
        """Refresh one kind's local map from the api. True if it changed. A failed fetch keeps the
        last known copy rather than dropping every alias."""
        try:
            aliases = await self.client.get_aliases(kind)
        except Exception:
            return False
        by_target: dict[str, list[str]] = {}
        for a in aliases:
            by_target.setdefault(a.target_id, []).append(a.alias)
        for values in by_target.values():
            values.sort()
        if by_target != self._aliases.get(kind, {}):
            self._aliases[kind] = by_target
            return True
        return False

    async def refresh_aliases(self) -> bool:
        changed = False
        for kind in ALIAS_KINDS:
            if await self._fetch_aliases_for(kind):
                changed = True
        if changed:
            self._save_aliases()
        return changed

    def add_alias_local(self, kind: str, target_id: str, alias: str) -> None:
        """Record an alias just added through the api so the local copy is current at once, without
        waiting for the next poll. `alias` is already preprocessed."""
        values = self._aliases.setdefault(kind, {}).setdefault(target_id, [])
        if alias not in values:
            values.append(alias)
            values.sort()
        self._save_aliases()

    def remove_alias_local(self, kind: str, target_id: str, alias: str) -> None:
        by_target = self._aliases.setdefault(kind, {})
        values = by_target.get(target_id)
        if values and alias in values:
            values.remove(alias)
            if not values:
                del by_target[target_id]
        self._save_aliases()

    def _save_aliases(self) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            with open(self._path(ALIAS_CACHE), "w", encoding="utf-8") as f:
                json.dump(self._aliases, f, ensure_ascii=False)
        except OSError as e:
            LOGGING.warnprint(f"holodori alias cache write failed: {e}")

    def _load_aliases(self) -> None:
        path = self._path(ALIAS_CACHE)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._aliases = {
                k: {t: list(v) for t, v in m.items()} for k, m in data.items()
            }
        except Exception as e:
            LOGGING.warnprint(f"holodori alias cache load failed: {e}")

    async def _poll_aliases(self) -> None:
        while True:
            await asyncio.sleep(ALIAS_REFRESH_INTERVAL)
            try:
                await self.refresh_aliases()
            except Exception as e:
                LOGGING.warnprint(f"holodori alias refresh failed: {e}")

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
        # keep only tracked event types (hasData is a safety net: never hide an event we actually
        # have leaderboard data for, even if its archived type is missing)
        evs = [e for e in evs if e.type in TRACKED_EVENT_TYPES or e.hasData]
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

    # --- alias-aware fuzzy matching (folds the local alias copy into the keys). NOT wired into any
    #     autocomplete/command yet; here for later. events pass their list in (they are per-region).

    def search_songs_aliased(self, query: str, limit: int = 25) -> list[Song]:
        return search.rank(
            query, self._songs, lambda s: (s.title, s.id, *self.song_aliases(s.id)), limit
        )

    def match_song_aliased(self, query: str) -> Song | None:
        return search.best_match(
            query, self._songs, lambda s: (s.title, s.id, *self.song_aliases(s.id))
        )

    def search_holomems_aliased(self, query: str, limit: int = 25) -> list[Holomem]:
        return search.rank(
            query,
            self._holomems,
            lambda h: (h.name, h.shortName, h.id, *self.holomem_aliases(h.id)),
            limit,
        )

    def match_holomem_aliased(self, query: str) -> Holomem | None:
        return search.best_match(
            query,
            self._holomems,
            lambda h: (h.name, h.shortName, h.id, *self.holomem_aliases(h.id)),
        )

    def search_events_aliased(
        self, events: list[EventInfo], query: str, limit: int = 25
    ) -> list[EventInfo]:
        return search.rank(
            query, events, lambda e: (e.name, e.eventId, *self.event_aliases(e.eventId)), limit
        )

    def match_event_aliased(self, events: list[EventInfo], query: str) -> EventInfo | None:
        return search.best_match(
            query, events, lambda e: (e.name, e.eventId, *self.event_aliases(e.eventId))
        )
