"""idempotent schema setup: `python -m scripts.database_setup`."""

import asyncio

from database.pool import close_pool, create_pool
from helpers.config_loader import get_config, set_config_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    discord_id BIGINT UNIQUE NOT NULL,
    settings JSONB,
    guess_stats JSONB,
    blacklisted BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS guilds (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT UNIQUE NOT NULL,
    guessing_enabled BOOLEAN DEFAULT TRUE
);
"""


async def _main() -> None:
    set_config_path("config.yml")
    get_config()
    pool = await create_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    await close_pool()
    print("schema ready")


if __name__ == "__main__":
    asyncio.run(_main())
