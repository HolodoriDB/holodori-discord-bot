"""http client for api.holodori.best.

every request to the api origin carries the cloudflare bypass header (config holodori.bypass_*),
without which the origin guard returns 403. the extracted-asset cdn is public and needs no header,
so image urls can go straight into discord embeds.
"""

import asyncio
from typing import Any
from urllib.parse import quote

import aiohttp

from data.models import (
    Alias,
    AliasAddResponse,
    AliasesResponse,
    AssetInfo,
    Card,
    CardDetail,
    EventInfo,
    Holomem,
    HolomemGroup,
    Song,
    SongDetail,
)
from helpers import unblock

__all__ = ["HolodoriClient", "HolodoriError", "HolodoriNotFound"]

_LANGS = {"eng", "jpn", "kor", "cht", "chs", "ind"}


def _resolve_langs(obj: Any, lang: str) -> Any:
    """collapse any {eng,jpn,...} lang-map to the requested language's string; recurse otherwise.

    some detail endpoints return raw masterdata where localized fields are still per-language dicts.
    """
    if isinstance(obj, dict):
        if not obj:
            return None  # empty lang-map / empty object -> no value
        if all(k in _LANGS for k in obj):
            return obj.get(lang) or obj.get("eng") or next(iter(obj.values()))
        return {k: _resolve_langs(v, lang) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_langs(v, lang) for v in obj]
    return obj


class HolodoriError(Exception):
    def __init__(self, status: int, detail: Any = "") -> None:
        self.status = status
        # structured errors (e.g. a taken alias -> {code, id, target_id}) keep their dict on `data`;
        # `detail` stays a plain string so existing `e.detail or e.status` messages read the same
        self.data: dict[str, Any] = detail if isinstance(detail, dict) else {}
        self.detail = str(self.data.get("code", "")) if self.data else str(detail)
        super().__init__(f"holodori API error {status}: {self.detail}")


class HolodoriNotFound(HolodoriError):
    """404 from the api."""


class HolodoriClient:
    def __init__(
        self,
        api_url: str,
        *,
        bypass_header: str = "",
        bypass_value: str = "",
        internal_token: str = "",
        lang: str = "eng",
    ) -> None:
        self.base = api_url.rstrip("/")
        self.lang = lang
        self._headers = {bypass_header: bypass_value} if bypass_header and bypass_value else {}
        self._internal_token = internal_token
        self._session: aiohttp.ClientSession | None = None
        self._asset_info: AssetInfo = AssetInfo()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    async def _get(self, path: str, *, _headers: dict[str, str] | None = None, **params: Any) -> Any:
        session = await self._ensure_session()
        clean = {k: v for k, v in params.items() if v is not None}
        async with session.get(self._url(path), params=clean, headers=_headers) as resp:
            if resp.status == 404:
                raise HolodoriNotFound(404, await _detail(resp))
            if resp.status >= 400:
                raise HolodoriError(resp.status, await _detail(resp))
            return await resp.json()

    async def _get_bytes(self, path: str, **params: Any) -> bytes:
        session = await self._ensure_session()
        clean = {k: v for k, v in params.items() if v is not None}
        async with session.get(self._url(path), params=clean) as resp:
            if resp.status == 404:
                raise HolodoriNotFound(404, await _detail(resp))
            if resp.status >= 400:
                raise HolodoriError(resp.status, await _detail(resp))
            return await resp.read()

    def _internal_headers(self) -> dict[str, str]:
        return {"x-internal-token": self._internal_token} if self._internal_token else {}

    async def _send(self, method: str, path: str, *, json: Any = None) -> Any:
        # POST/DELETE with the internal token; used only for the bot-only alias routes
        session = await self._ensure_session()
        async with session.request(
            method, self._url(path), json=json, headers=self._internal_headers()
        ) as resp:
            if resp.status == 404:
                raise HolodoriNotFound(404, await _detail(resp))
            if resp.status >= 400:
                raise HolodoriError(resp.status, await _detail(resp))
            return await resp.json()

    # --- assets ---

    async def get_asset_info(self) -> AssetInfo:
        self._asset_info = AssetInfo.model_validate(await self._get("/api/asset_hashes"))
        return self._asset_info

    async def ensure_asset_info(self) -> None:
        # asset info (incl. the public cdn base) loads in the background after startup; a command
        # that builds public asset urls before that must load it, or asset_url falls back to the
        # gated /api/asset path that discord can't fetch
        if not self._asset_info.cdn:
            try:
                await self.get_asset_info()
            except HolodoriError:
                pass

    def asset_url(self, path: str) -> str:
        """build a public url for an asset path (caller includes the extension, e.g. '<thumb>.webp').

        hash key is the first two path segments joined, matching the frontend.
        """
        info = self._asset_info
        key = "/".join(path.split("/")[:2])
        h = info.hashes.get(key)
        if info.cdn:
            url = f"{info.cdn}/assets/{path}"
        else:
            url = f"{self.base}/api/asset/{path}"
        return f"{url}?hash={h}" if h else url

    def image_url(self, path: str | None) -> str | None:
        return self.asset_url(f"{path}.webp") if path else None

    # --- aliases (bot-only: every call carries the internal token) ---

    async def get_aliases(self, kind: str) -> list[Alias]:
        return AliasesResponse.model_validate(
            await self._get(f"/api/aliases/{kind}", _headers=self._internal_headers())
        ).aliases

    async def add_alias(
        self, kind: str, target_id: str, alias: str, region: str | None = None
    ) -> AliasAddResponse:
        return AliasAddResponse.model_validate(
            await self._send(
                "POST",
                f"/api/aliases/{kind}",
                json={"target_id": target_id, "alias": alias, "region": region},
            )
        )

    async def remove_alias(self, kind: str, alias_id: int) -> None:
        await self._send("DELETE", f"/api/aliases/{kind}", json={"alias_id": alias_id})

    async def get_search_index(self, kind: str) -> dict[str, list[str]]:
        # {id: [auto search keys]} = all-language names + romanizations (deduped), for the matcher
        data = await self._get(f"/api/search_index/{kind}")
        return data.get("keys", {})

    def unsquished_image_url(self, path: str | None) -> str | None:
        # squished-into-pot art (card full art, event logo/banner) has a _unsquished.webp sibling
        # baked at its true aspect by the extractor; point straight at it, no client-side unsquish.
        return self.asset_url(f"{path}_unsquished.webp") if path else None

    def manual_asset(self, name: str) -> str:
        v = self._asset_info.manual
        url = f"{self.base}/manual_assets/{name}"
        return f"{url}?v={v}" if v else url

    def chart_image_url(self, song_id: str, difficulty: str) -> str:
        # the pre-rendered chart png lives inside the .sus bundle dir on the cdn
        path = f"resources/chart_{song_id}_{difficulty}.sus/chart_{song_id}_{difficulty}.png"
        return self.asset_url(path)

    async def fetch_bytes(self, url: str) -> bytes:
        session = await self._ensure_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    # --- cards ---

    async def get_cards(self, lang: str | None = None) -> list[Card]:
        data = await self._get("/api/cards", lang=lang or self.lang)
        raw = data["cards"]
        return await asyncio.get_running_loop().run_in_executor(
            unblock.executor, lambda: [Card.model_validate(c) for c in raw]
        )

    async def get_card(self, card_id: str, lang: str | None = None) -> CardDetail:
        lang = lang or self.lang
        raw = await self._get(f"/api/cards/{card_id}", lang=lang)
        return CardDetail.model_validate(_resolve_langs(raw, lang))

    # --- songs ---

    async def get_songs(self, lang: str | None = None) -> list[Song]:
        raw = await self._get("/api/songs", lang=lang or self.lang)
        return await asyncio.get_running_loop().run_in_executor(
            unblock.executor, lambda: [Song.model_validate(s) for s in raw]
        )

    async def get_song(self, music_id: str, lang: str | None = None) -> SongDetail:
        lang = lang or self.lang
        raw = await self._get(f"/api/songs/{music_id}", lang=lang)
        return SongDetail.model_validate(_resolve_langs(raw, lang))

    async def get_song_audio(self, music_id: str) -> bytes:
        return await self._get_bytes(f"/api/songs/{quote(music_id)}/audio")

    # --- holomems ---

    async def get_holomems(self) -> list[Holomem]:
        data = await self._get("/api/holomem/list")
        return [Holomem.model_validate(h) for h in data["holomems"]]

    async def get_holomem_groups(self, lang: str | None = None) -> list[HolomemGroup]:
        data = await self._get("/api/holomem/info", lang=lang or self.lang)
        return [HolomemGroup.model_validate(g) for g in data["groups"]]

    async def get_holomem_leaderboard(self, region: str, character_id: str) -> dict:
        return await self._get(
            "/api/holomem/leaderboard", region=region, character_id=character_id
        )

    # --- events ---

    async def get_events(self, region: str, language: str | None = None) -> list[EventInfo]:
        data = await self._get("/api/events/list", region=region, language=language or self.lang)
        return [EventInfo.model_validate(e) for e in data["events"]]

    async def get_event_leaderboard(
        self,
        region: str,
        *,
        event_id: str | None = None,
        chapter_id: str | None = None,
        language: str | None = None,
        music: bool | None = None,
        music_id: str | None = None,
        at: int | None = None,
    ) -> dict:
        return await self._get(
            "/api/events/leaderboard",
            region=region,
            event_id=event_id,
            chapter_id=chapter_id,
            language=language or self.lang,
            music=str(music).lower() if music is not None else None,
            music_id=music_id,
            at=at,
        )

    async def get_event_leaderboard_ids(
        self,
        region: str,
        *,
        event_id: str | None = None,
        chapter_id: str | None = None,
        language: str | None = None,
    ) -> dict:
        # authorized variant: sends the shared internal token so each row keeps its stable userId
        # (the public leaderboard omits it). used by the ranking tracker to follow a player.
        headers = {"x-internal-token": self._internal_token} if self._internal_token else None
        return await self._get(
            "/api/events/leaderboard",
            _headers=headers,
            region=region,
            event_id=event_id,
            chapter_id=chapter_id,
            language=language or self.lang,
        )

    async def get_event_graph(
        self,
        region: str,
        rank: int,
        *,
        event_id: str | None = None,
        chapter_id: str | None = None,
        border: bool | None = None,
        music: bool | None = None,
        music_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        return await self._get(
            "/api/events/graph",
            region=region,
            rank=rank,
            event_id=event_id,
            chapter_id=chapter_id,
            border=str(border).lower() if border is not None else None,
            music=str(music).lower() if music is not None else None,
            music_id=music_id,
            user_id=user_id,
        )

    async def get_event_tiers(self) -> list[dict]:
        data = await self._get("/api/events/tiers")
        return data.get("tiers") or [] if isinstance(data, dict) else []

    async def search_players(self, query: str, region: str | None = None) -> list[dict]:
        # fuzzy player lookup over the current top-100 (all regions unless one given); romanized
        data = await self._get("/api/events/player_search", q=query, region=region)
        return data.get("players") or [] if isinstance(data, dict) else []

    async def get_profile(self, public_id: str, region: str = "us", lang: str | None = None) -> dict:
        # full profile by public id (8-char friend code); {region, publicUserId, profile:{...}}
        return await self._get("/api/profile", id=public_id, region=region, lang=lang or self.lang)

    async def get_profile_palette(self, public_id: str, region: str = "us") -> bytes:
        # the proxied gated custom-palette jpg bytes (only for profiles with a custom palette set)
        return await self._get_bytes("/api/profile/palette", id=public_id, region=region)

    # --- misc data ---

    async def get_items(self, lang: str | None = None) -> list[dict]:
        data = await self._get("/api/items", lang=lang or self.lang)
        return data["items"]


async def _detail(resp: aiohttp.ClientResponse) -> Any:
    try:
        body = await resp.json()
        if isinstance(body, dict):
            return body.get("detail", body)
        return body
    except Exception:
        return ""
