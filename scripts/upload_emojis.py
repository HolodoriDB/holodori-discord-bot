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

                # holomem fanmark emojis (chr_<num>_fanmark), one per holomem, from the public
                # extracted-asset cdn (img_chr_motif_sub_<num>). used as the event chapter-button emojis.
                info: dict = {}
                rows: list = []
                try:
                    async with sess.get(f"{base}/api/asset_hashes") as r:
                        r.raise_for_status()
                        info = await r.json()
                    async with sess.get(f"{base}/api/holomem/list") as r:
                        r.raise_for_status()
                        holomems = await r.json()
                    rows = holomems if isinstance(holomems, list) else holomems.get("holomems", [])
                except Exception as e:
                    print(f"fanmark fetch skipped: {e}")
                cdn = info.get("cdn") or base
                hashes = info.get("hashes") or {}
                # one per holomem, plus the official "verified" mark (fan_mark-official -> 99999,
                # not a holomem so it isn't in /holomem/list)
                nums = [cid.split("-")[-1] for hm in rows if (cid := hm.get("id"))]
                nums.append("99999")
                for num in nums:
                    name = f"chr_{num}_fanmark"
                    emoji = existing.get(name)
                    if emoji and not force:
                        result[name] = _record(emoji)
                        continue
                    if emoji and force:
                        await emoji.delete()
                    asset = f"assetbundles/img_chr_motif_sub_{num}/img_chr_motif_sub_{num}"
                    h = hashes.get(f"assetbundles/img_chr_motif_sub_{num}")
                    url = f"{cdn}/assets/{asset}.webp" + (f"?hash={h}" if h else "")
                    try:
                        async with sess.get(url) as r:
                            if r.status != 200:
                                print(f"skip {name}: cdn {r.status}")
                                continue
                            img = _to_discord_image(await r.read())
                        emoji = await client.create_application_emoji(name=name, image=img)
                        result[name] = _record(emoji)
                        print(f"uploaded {name} -> {emoji}")
                    except Exception as e:
                        print(f"fanmark {name} failed: {e}")
        finally:
            with open(EMOJIS_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            await client.close()

    await client.start(cfg["discord"]["token"])


if __name__ == "__main__":
    asyncio.run(_main())
