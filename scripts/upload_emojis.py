"""sync app emojis from the holodori manual_assets api: `python -m scripts.upload_emojis`.

reuses any application emoji that already exists (by name) and only uploads the missing ones, then
writes data/emojis.json. pass `--force` to re-upload (delete + recreate) every emoji.
"""

import asyncio
import io
import json
import sys

import aiohttp
import discord
from PIL import Image, ImageSequence

from helpers.config_loader import get_config, set_config_path
from helpers.emojis import DEFS, EMOJIS_FILE


def _to_discord_image(webp: bytes) -> bytes:
    # discord emojis accept png/jpeg/gif only, so convert (animated webp -> gif)
    im = Image.open(io.BytesIO(webp))
    out = io.BytesIO()
    if getattr(im, "n_frames", 1) > 1:
        frames = [f.convert("RGBA") for f in ImageSequence.Iterator(im)]
        frames[0].save(
            out, format="GIF", save_all=True, append_images=frames[1:], loop=0, disposal=2
        )
    else:
        im.convert("RGBA").save(out, format="PNG")
    return out.getvalue()


def _record(emoji: discord.Emoji) -> dict:
    return {"id": emoji.id, "animated": emoji.animated, "mention": str(emoji)}


async def _main() -> None:
    set_config_path("config.yml")
    cfg = get_config()
    force = "--force" in sys.argv
    base = cfg["holodori"]["api_url"].rstrip("/")
    headers = {cfg["holodori"]["bypass_header"]: cfg["holodori"]["bypass_value"]}

    client = discord.Client(intents=discord.Intents.none())
    result: dict[str, dict] = {}

    @client.event
    async def on_ready() -> None:
        try:
            existing = {e.name: e for e in await client.fetch_application_emojis()}
            async with aiohttp.ClientSession(headers=headers) as sess:
                for name, path in DEFS.items():
                    emoji = existing.get(name)
                    if emoji and not force:
                        result[name] = _record(emoji)
                        print(f"exists, reusing {name}")
                        continue
                    if emoji and force:
                        await emoji.delete()
                    async with sess.get(f"{base}/manual_assets/{path}") as r:
                        r.raise_for_status()
                        img = _to_discord_image(await r.read())
                    emoji = await client.create_application_emoji(name=name, image=img)
                    result[name] = _record(emoji)
                    print(f"uploaded {name} -> {emoji}")
        finally:
            with open(EMOJIS_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            await client.close()

    await client.start(cfg["discord"]["token"])


if __name__ == "__main__":
    asyncio.run(_main())
