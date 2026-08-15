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

CREATE TABLE IF NOT EXISTS room_renames (
    channel_id BIGINT PRIMARY KEY,
    times DOUBLE PRECISION[] NOT NULL DEFAULT '{}'
);

-- channel-wide live leaderboard subscriptions (/tracking); tracking_type is 2 (every tick) or 60 (hourly)
CREATE TABLE IF NOT EXISTS event_trackers (
    channel_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    region TEXT NOT NULL,
    tracking_type INT NOT NULL,
    last_post DOUBLE PRECISION NOT NULL DEFAULT 0
);
-- migration for tables created before last_post existed
ALTER TABLE event_trackers ADD COLUMN IF NOT EXISTS last_post DOUBLE PRECISION NOT NULL DEFAULT 0;

-- per tier / per user alert subscriptions (/track); config holds tier, cutoff, min, max, followed
-- userId, name, last_score, last_rank
CREATE TABLE IF NOT EXISTS track_alerts (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    region TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'
);

-- per channel co-op room waitlist queues (/waitlist); users is the queue order, leavers maps a
-- discord id to the unix time they expect to leave
CREATE TABLE IF NOT EXISTS waitlists (
    channel_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    song TEXT,
    users BIGINT[] NOT NULL DEFAULT '{}',
    leavers JSONB NOT NULL DEFAULT '{}',
    message_id BIGINT,
    last_use DOUBLE PRECISION NOT NULL DEFAULT 0
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
