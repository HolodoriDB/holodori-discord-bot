import json
from typing import Any

import asyncpg

SETTING_DEFAULTS: dict[str, Any] = {
    "default_language": "eng",  # eng/jpn/kor/cht/chs/ind
    "default_region": "us",  # event region us/as/jp
    "timezone": "et",
    "first_time_guess_end": True,
    "opt_out_rolling_guess_leaderboards": False,
}

GUESS_DEFAULT = {"fail": 0, "success": 0, "ragequit": 0, "hint": 0}

GUESS_LEADERBOARD_PER_PAGE = 20


class UserData:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.db = pool

    async def verify_discord_user(self, user_id: int) -> None:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM users WHERE discord_id = $1", user_id)
            if not row:
                await conn.execute("INSERT INTO users (discord_id) VALUES ($1)", user_id)

    async def verify_discord_guild(self, guild_id: int) -> None:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM guilds WHERE guild_id = $1", guild_id)
            if not row:
                await conn.execute("INSERT INTO guilds (guild_id) VALUES ($1)", guild_id)

    # --- bans ---

    async def set_banned(self, user_id: int, blacklisted: bool) -> None:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            await conn.execute(
                "UPDATE users SET blacklisted = $1 WHERE discord_id = $2", blacklisted, user_id
            )

    async def get_banned(self, user_id: int) -> bool:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT blacklisted FROM users WHERE discord_id = $1", user_id
            )
            return bool(result["blacklisted"]) if result and result["blacklisted"] else False

    # --- settings (jsonb, prunes unknown keys) ---

    async def get_settings(self, user_id: int, key: str | None = None) -> Any:
        await self.verify_discord_user(user_id)
        assert key in SETTING_DEFAULTS or key is None
        if key:
            key = key.lower().strip()
        async with self.db.acquire() as conn:
            result = await conn.fetchrow("SELECT settings FROM users WHERE discord_id = $1", user_id)
            stuff = json.loads(result["settings"]) if result and result["settings"] else {}
            if key:
                return stuff.get(key, SETTING_DEFAULTS[key])
            return {k: stuff.get(k, v) for k, v in SETTING_DEFAULTS.items()}

    async def change_settings(self, user_id: int, key: str, value: Any) -> dict:
        await self.verify_discord_user(user_id)
        key = key.lower().strip()
        assert key in SETTING_DEFAULTS
        async with self.db.acquire() as conn:
            result = await conn.fetchrow("SELECT settings FROM users WHERE discord_id = $1", user_id)
            stuff = json.loads(result["settings"]) if result and result["settings"] else {}
            stuff[key] = value
            for k in list(stuff.keys()):
                if k not in SETTING_DEFAULTS:
                    stuff.pop(k, None)
            await conn.execute(
                "UPDATE users SET settings = $1 WHERE discord_id = $2", json.dumps(stuff), user_id
            )
            return stuff

    # --- guess stats (per mode) ---

    async def get_guesses(self, user_id: int, key: str | None = None) -> dict:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guess_stats FROM users WHERE discord_id = $1", user_id
            )
            stuff = json.loads(result["guess_stats"]) if result and result["guess_stats"] else {}
            return stuff.get(key, dict(GUESS_DEFAULT)) if key else stuff

    async def add_guesses(self, user_id: int, key: str, stat: str) -> dict:
        await self.verify_discord_user(user_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guess_stats FROM users WHERE discord_id = $1", user_id
            )
            guess_stats = (
                json.loads(result["guess_stats"]) if result and result["guess_stats"] else {}
            )
            if key not in guess_stats:
                guess_stats[key] = dict(GUESS_DEFAULT)
            if stat in guess_stats[key]:
                guess_stats[key][stat] += 1
            await conn.execute(
                "UPDATE users SET guess_stats = $1 WHERE discord_id = $2",
                json.dumps(guess_stats),
                user_id,
            )
            return guess_stats[key]

    async def get_guesses_leaderboard(self, guess_type: str, page: int):
        per_page = GUESS_LEADERBOARD_PER_PAGE
        total_result = await self.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE guess_stats ? $1", guess_type
        )
        total_pages = (total_result + per_page - 1) // per_page
        if page > total_pages and total_pages > 0:
            page = total_pages
        leaderboard = await self.db.fetch(
            f"""
            SELECT id, discord_id,
                guess_stats->'{guess_type}'->>'success' AS success,
                CAST(guess_stats->'{guess_type}'->>'success' AS INT) AS score
            FROM users WHERE guess_stats ? $1
            ORDER BY score DESC, id ASC
            LIMIT $2 OFFSET $3
            """,
            guess_type,
            per_page,
            (page - 1) * per_page,
        )
        return leaderboard, total_pages

    # --- per-guild config ---

    async def toggle_guessing(self, guild_id: int, enabled: bool) -> bool:
        await self.verify_discord_guild(guild_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "UPDATE guilds SET guessing_enabled = $1 WHERE guild_id = $2 "
                "RETURNING guessing_enabled",
                enabled,
                guild_id,
            )
            return result["guessing_enabled"] if result else True

    async def guessing_enabled(self, guild_id: int) -> bool:
        await self.verify_discord_guild(guild_id)
        async with self.db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT guessing_enabled FROM guilds WHERE guild_id = $1", guild_id
            )
            return result["guessing_enabled"] if result else True
