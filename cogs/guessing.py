from __future__ import annotations

import asyncio
import io
import random
import re
import time
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import embeds, imaging
from helpers.emojis import emojis
from helpers.views import HoloView
from services import song_clip
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

GUESS_PREFIX = "-"
GUESS_TIME = 60
MODE_TIME = {
    "character": 30,
    "character_bw": 30,
    "chart": 90,
    "chart_hard": 90,
    "music": 300,
}
GUESS_TIP = "\n-# Use `-hint` for a hint, `-end` to give up, or `-time` for time left!"
MUSIC_TIP = "\n-# Use `-hint` to provide more of the song, or `-time` for time left!"
CONTROL_LINE = (
    "Use `-your guess` to guess, `-hint` for a hint, `-end` to give up, or `-time` for time left."
)
HINT_COOLDOWN = 2.0
MAX_TEXT_HINTS = 3
REVEAL_FRACTION = 1 / 7
STAGE_SECONDS = {1: 1.0, 2: 3.0, 3: 5.0, 4: 8.0}
MAX_STAGE = 4
GIVEUP_FRACTION = 1 / 4  # non-starter can end after this fraction of the round
MUSIC_GIVEUP_FRACTION = 1 / 15
PERM_CACHE_TTL = 300.0  # fallback refetch window; the permissions-update event invalidates sooner

# mode -> (guessType, "Guess The X" title, prompt line)
MODES: dict[str, tuple[str, str, str]] = {
    "jacket": ("song", "Guess The Song", "Guess the song from a cropped jacket."),
    "jacket_30px": ("song", "Guess The Song", "**30px Jacket!** Guess the song from a cropped jacket."),
    "jacket_bw": ("song", "Guess The Song", "**Grayscale Jacket!** Guess the song from a cropped jacket."),
    "jacket_challenge": ("song", "Guess The Song", "**CHALLENGE!** Cropped grayscale 30px jacket."),
    "character": ("character", "Guess The Character", "Guess the holomem from a cropped card."),
    "character_bw": ("character", "Guess The Character", "**Grayscale Card!** Guess the holomem from a cropped card."),
    "chart": ("song", "Guess The Chart", "Guess the song from a cropped Expert chart."),
    "chart_hard": ("song", "Guess The Chart", "Guess the song from a cropped Hard chart."),
    "notes": ("song", "Guess The Song", "Guess the song from its note count."),
    "music": ("song", "Guess The Music", "Guess the song from a clip."),
    "event_background": ("event", "Guess The Event", "Guess the event from a cropped banner."),
}
CHART_DIFF = {"chart": "expert", "chart_hard": "hard"}


def _giveup_gate(data: dict) -> float:
    total = MODE_TIME.get(data["guessing"], GUESS_TIME)
    frac = MUSIC_GIVEUP_FRACTION if data["guessing"] == "music" else GIVEUP_FRACTION
    return total * frac


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _masked_name(name: str, frac: float) -> str:
    positions = [i for i, ch in enumerate(name) if not ch.isspace()]
    count = max(1, round(len(positions) * frac))
    revealed = set(random.sample(positions, min(count, len(positions))))
    return "".join(ch if (ch.isspace() or i in revealed) else "_" for i, ch in enumerate(name))


class GuessCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot
        self.rounds: dict[int, dict] = bot.cache.guess_channels
        # guild_id -> (fetched_at, the guess group's discord command permissions). dropped on the
        # permissions-update event so an admin toggling the command in a channel applies at once.
        self._perm_cache: dict[int, tuple[float, list[discord.app_commands.AppCommandPermissions]]] = {}
        self.timeout_task.start()

    async def cog_unload(self) -> None:
        self.timeout_task.cancel()

    guess = app_commands.Group(
        name="guess",
        description="Guessing games.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    # --- picking / building ---

    async def _fetch(self, url: str | None) -> bytes | None:
        if not url or not self.bot.holo:
            return None
        try:
            return await self.bot.holo.fetch_bytes(url)
        except Exception:
            return None

    def _diff_level(self, song, diff: str) -> int | None:
        return next((d.level for d in song.difficulties if d.type == diff), None)

    def _pool_songs(self) -> list:
        # exclude unreleased/future content from guessing answers (the m9999 placeholder is already
        # dropped globally by the data store, so it can't be an answer or a guess match)
        assert self.bot.data
        now = time.time() * 1000
        return [s for s in self.bot.data.songs() if not s.startTime or s.startTime <= now]

    async def _build(self, mode: str) -> dict | None:
        assert self.bot.data and self.bot.holo
        guess_type = MODES[mode][0]
        if guess_type == "character":
            return await self._build_character(mode)
        if guess_type == "event":
            return await self._build_event(mode)
        return await self._build_song(mode)

    def _base(self, mode: str, guess_type: str) -> dict:
        return {
            "guessing": mode,
            "guessType": guess_type,
            "startTime": None,
            "createdAt": time.time(),
            "guessers": set(),
            "data": {"hint_stage": 0, "last_hint": 0.0},
        }

    async def _build_song(self, mode: str) -> dict | None:
        assert self.bot.data and self.bot.holo
        holo = self.bot.holo
        songs = self._pool_songs()
        random.shuffle(songs)

        if mode in CHART_DIFF:
            diff = CHART_DIFF[mode]
            for s in songs[:8]:
                raw = await self._fetch(holo.chart_image_url(s.id, diff))
                if not raw:
                    continue
                img = await asyncio.to_thread(imaging.crop_chart, raw)
                return self._song_round(mode, s, image=img)
            return None

        if mode == "notes":
            for s in songs[:6]:
                try:
                    detail = await holo.get_song(s.id)
                except HolodoriError:
                    continue
                expert = next(
                    (d for d in detail.difficulties if d.difficultyType.endswith("EXPERT")), None
                )
                if expert and expert.fullComboNoteCount:
                    r = self._song_round(mode, s, image=None)
                    r["data"]["notes"] = expert.fullComboNoteCount
                    return r
            return None

        if mode == "music":
            if not song_clip.HAS_FFMPEG:
                return None
            for s in songs[:6]:
                full = await self._fetch(f"{holo.base}/api/songs/{s.id}/audio")
                if not full:
                    continue
                length = s.length or 40
                if length < 26:
                    continue
                window = random.uniform(5.0, length - 20.0)
                clip = await song_clip.clip(full, window, STAGE_SECONDS[1])
                if not clip:
                    continue
                r = self._song_round(mode, s, image=None)
                r["data"].update({"audio": clip, "audio_full": full, "window": window, "stage": 1})
                return r
            return None

        # jacket family
        for s in songs[:8]:
            raw = await self._fetch(holo.image_url(s.jacket))
            if not raw:
                continue
            if mode == "jacket_30px":
                img = await asyncio.to_thread(imaging.crop_square, raw, size=30)
            elif mode == "jacket_bw":
                img = await asyncio.to_thread(imaging.crop_square, raw, size=250, grayscale=True)
            elif mode == "jacket_challenge":
                img = await asyncio.to_thread(imaging.crop_square, raw, size=30, grayscale=True)
            else:
                img = await asyncio.to_thread(imaging.crop_square, raw, size=250)
            return self._song_round(mode, s, image=img)
        return None

    def _song_round(self, mode: str, song, *, image: bytes | None) -> dict:
        assert self.bot.holo
        r = self._base(mode, "song")
        r["answer"] = song.id
        r["answerName"] = song.title
        if mode in CHART_DIFF:
            r["reveal_image"] = self.bot.holo.chart_image_url(song.id, CHART_DIFF[mode])
        else:
            r["reveal_image"] = self.bot.holo.image_url(song.jacket)
        r["data"]["image"] = image
        return r

    async def _build_character(self, mode: str) -> dict | None:
        assert self.bot.data and self.bot.holo
        holo = self.bot.holo
        cards = [c for c in self.bot.data.cards() if c.image and c.rarity >= 3]
        random.shuffle(cards)
        for card in cards[:8]:
            art_url = holo.unsquished_image_url(card.image)
            raw = await self._fetch(art_url)
            if not raw:
                continue
            img = await asyncio.to_thread(
                imaging.crop_square, raw, size=250, grayscale=(mode == "character_bw")
            )
            member = self.bot.data.get_holomem(card.characterId)
            r = self._base(mode, "character")
            r["answer"] = card.character
            r["answerName"] = card.character
            r["holomem_id"] = card.characterId
            r["card_id"] = card.id
            r["reveal_image"] = art_url  # full (unsquished) card art -> main reveal image
            r["reveal_thumb"] = holo.image_url(card.thumb)  # card thumbnail -> embed thumbnail slot
            r["data"].update(
                {
                    "image": img,
                    "card_name": card.name,
                    "rarity": card.rarity,
                    "attr": card.attributeName,
                    "group": card.group.name if card.group else None,
                    "names": {normalize(card.character)}
                    | ({normalize(member.name), normalize(member.shortName or "")} if member else set()),
                }
            )
            return r
        return None

    async def _build_event(self, mode: str) -> dict | None:
        assert self.bot.data and self.bot.holo
        holo = self.bot.holo
        try:
            events = [e for e in await self.bot.data.events("us", holo.lang) if e.banner]
        except HolodoriError:
            return None
        random.shuffle(events)
        for ev in events[:6]:
            raw = await self._fetch(holo.unsquished_image_url(ev.banner))
            if not raw:
                continue
            img = await asyncio.to_thread(imaging.crop_square, raw, size=250)
            r = self._base(mode, "event")
            r["answer"] = ev.eventId
            r["answerName"] = ev.name
            r["region"] = "us"
            r["reveal_image"] = holo.unsquished_image_url(ev.logo)  # event logo -> main reveal image
            r["data"]["image"] = img
            r["data"]["event_type"] = "Song Score" if ev.isSongScore else "Marathon"
            return r
        return None

    # --- command permission gate ---

    @commands.Cog.listener()
    async def on_raw_app_command_permissions_update(
        self, payload: discord.RawAppCommandPermissionsUpdateEvent
    ) -> None:
        if payload.guild is not None:
            self._perm_cache.pop(payload.guild.id, None)

    @staticmethod
    def _channel_allows(
        perms: list[discord.app_commands.AppCommandPermissions],
        channel: "discord.abc.GuildChannel | discord.Thread",
        guild_id: int,
    ) -> bool:
        # discord's channel model: default allowed, an "all channels" entry (id == guild_id - 1)
        # sets the baseline, a specific channel entry overrides it. threads inherit their parent.
        ids = {channel.id}
        parent = getattr(channel, "parent_id", None)
        if parent:
            ids.add(parent)
        all_channels = guild_id - 1
        allowed = True
        specific: bool | None = None
        for p in perms:
            if p.type is not discord.AppCommandPermissionType.channel:
                continue
            if p.id in ids:
                specific = p.permission
            elif p.id == all_channels:
                allowed = p.permission
        return specific if specific is not None else allowed

    async def _guess_denied_here(self, interaction: discord.Interaction) -> bool:
        """True when the guess group is denied in this channel by discord's command permissions.
        admins bypass the client-side block (their reply just gets forced ephemeral) and buttons
        aren't commands at all, so we enforce it. fails open if the permissions can't be resolved."""
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None:
            return False
        cmd = next(
            (c for c in (getattr(self.bot, "app_commands", None) or []) if c.name == self.guess.name),
            None,
        )
        if cmd is None:
            return False
        cached = self._perm_cache.get(guild.id)
        if cached and time.time() - cached[0] < PERM_CACHE_TTL:
            perms = cached[1]
        else:
            try:
                perms = (await cmd.fetch_permissions(guild)).permissions
            except discord.NotFound:
                perms = []  # no overrides configured -> allowed everywhere
            except discord.HTTPException:
                return False  # can't tell -> don't block
            self._perm_cache[guild.id] = (time.time(), perms)
        return not self._channel_allows(perms, channel, guild.id)  # type: ignore[arg-type]

    # --- start ---

    async def _start(self, interaction: discord.Interaction, mode: str) -> None:
        channel_id = interaction.channel_id
        if channel_id is None:
            return
        if interaction.guild and self.bot.user_data:
            if not await self.bot.user_data.guessing_enabled(interaction.guild.id):
                await interaction.response.send_message(
                    embed=embeds.error_embed("Guessing is disabled in this server."), ephemeral=True
                )
                return
        if await self._guess_denied_here(interaction):
            await interaction.response.send_message(
                embed=embeds.error_embed("Guessing is disabled in this channel."), ephemeral=True
            )
            return
        if channel_id in self.rounds:
            await interaction.response.send_message(
                embed=embeds.error_embed("A guessing game is already happening here!")
            )
            return
        # only guild channels can withhold these; in a dm the bot always has them (and discord may
        # report app_permissions as empty there, which would false-trip this and block dm guessing)
        perms = interaction.app_permissions
        if interaction.guild is not None and not (
            perms.send_messages and perms.embed_links and perms.attach_files
        ):
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    "I need **Send Messages**, **Embed Links**, and **Attach Files** here to run a round."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            data = await self._build(mode)
        except Exception:
            data = None
        if data is None:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't start that round (asset unavailable). Try again.")
            )
            return

        secs = MODE_TIME.get(mode, GUESS_TIME)
        if mode == "notes":
            prompt = f"This song's Expert chart has **`{data['data']['notes']}`** notes."
        else:
            prompt = MODES[mode][2]
        giveup = round(_giveup_gate(data))
        note = f"\n-# You can give up in `{giveup}` seconds." if giveup else ""
        embed = embeds.embed(
            title=MODES[mode][1],
            description=f"{prompt} You have `{secs}` seconds.\n\n{CONTROL_LINE}{note}",
            color=discord.Color.blue(),
        )
        files: list[discord.File] = []
        if data["data"].get("image"):
            embed.set_image(url="attachment://image.png")
            files.append(discord.File(io.BytesIO(data["data"]["image"]), "image.png"))
        if data["data"].get("audio"):
            files.append(discord.File(io.BytesIO(data["data"]["audio"]), "clip.mp3"))
        msg = await interaction.followup.send(embed=embed, files=files, wait=True)
        # backstop: if discord still forced the puzzle ephemeral (e.g. perms couldn't be resolved
        # above), no one else can see or play it - turn it into a notice and don't register a
        # hidden round rather than leave one running that only the starter can see.
        if msg.flags.ephemeral:
            await msg.edit(
                embed=embeds.error_embed("Guessing is disabled in this channel."), attachments=[]
            )
            return
        data["starter"] = interaction.user.id
        data["startTime"] = time.time()
        data["channel"] = interaction.channel
        self.rounds[channel_id] = data

    # --- reveal ---

    def _reveal_extras(self, data: dict) -> str:
        extra = ""
        if data["data"].get("notes"):
            extra += f"\n\n**This song's Expert chart has `{data['data']['notes']}` notes.**"
        if data["guessType"] == "character" and data["data"].get("card_name"):
            extra += f"\n**Card:** {data['data']['card_name']}"
        return extra

    def _reveal_media(self, data: dict, embed: discord.Embed) -> list[discord.File]:
        # every reveal image is a cdn url now (squished art points at its _unsquished sibling); the
        # card thumbnail rides in the thumbnail slot, the main art in the image slot.
        if data.get("reveal_thumb"):
            embed.set_thumbnail(url=data["reveal_thumb"])
        if data.get("reveal_image"):
            embed.set_image(url=data["reveal_image"])
        return []

    def _reveal_embed(
        self, data: dict, *, title: str, description: str, color
    ) -> tuple[discord.Embed, list[discord.File]]:
        embed = embeds.embed(title=title, description=description, color=color)
        return embed, self._reveal_media(data, embed)

    def _time_embed(self, data: dict) -> discord.Embed:
        if not data.get("startTime"):
            return embeds.embed(title="Time Remaining", description="The round is still starting.")
        end = int(data["startTime"] + MODE_TIME.get(data["guessing"], GUESS_TIME))
        return embeds.embed(title="Time Remaining", description=f"This guess ends <t:{end}:R>.")

    async def _award(self, message: discord.Message, data: dict) -> None:
        self.rounds.pop(message.channel.id, None)
        started = data.get("startTime")
        timing = f" in `{time.time() - started:.2f}` seconds" if started else ""
        desc = f"Successfully guessed **`{data['answerName']}`**{timing}!" + self._reveal_extras(data)
        if self.bot.user_data:
            await self.bot.user_data.add_guesses(message.author.id, data["guessing"], "success")
        embed = embeds.success_embed(title="Correct", description=desc)
        files = self._reveal_media(data, embed)
        view = _GuessResultView(self, data)
        view.message = await message.reply(embed=embed, files=files, view=view)

    # --- timeout loop ---

    @tasks.loop(seconds=2)
    async def timeout_task(self) -> None:
        now = time.time()
        for channel_id, data in list(self.rounds.items()):
            if not data.get("startTime"):
                if now - data.get("createdAt", now) > 30:
                    self.rounds.pop(channel_id, None)
                continue
            if data["startTime"] + MODE_TIME.get(data["guessing"], GUESS_TIME) >= now:
                continue
            self.rounds.pop(channel_id, None)
            channel = data.get("channel") or self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                continue
            embed, files = self._reveal_embed(
                data,
                title="Failed",
                description=f"You failed to guess the {data['guessType']}.",
                color=discord.Color.red(),
            )
            embed.add_field(
                name="Answer",
                value=f"The correct answer was **{data['answerName']}**." + self._reveal_extras(data),
                inline=False,
            )
            try:
                view = _GuessResultView(self, data)
                view.message = await channel.send(embed=embed, files=files, view=view)
            except discord.HTTPException:
                pass

    @timeout_task.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # --- mode commands ---

    async def _cmd(self, interaction: discord.Interaction, mode: str) -> None:
        await self._start(interaction, mode)

    @guess.command(name="jacket", description="Guess the song from its jacket.")
    async def _jacket(self, i: discord.Interaction) -> None:
        await self._cmd(i, "jacket")

    @guess.command(name="jacket_30px", description="Guess the song from a pixelated jacket.")
    async def _jacket_30px(self, i: discord.Interaction) -> None:
        await self._cmd(i, "jacket_30px")

    @guess.command(name="jacket_bw", description="Guess the song from a grayscale jacket.")
    async def _jacket_bw(self, i: discord.Interaction) -> None:
        await self._cmd(i, "jacket_bw")

    @guess.command(name="jacket_challenge", description="Guess the song from a tiny jacket crop.")
    async def _jacket_challenge(self, i: discord.Interaction) -> None:
        await self._cmd(i, "jacket_challenge")

    @guess.command(name="character", description="Guess the holomem from card art.")
    async def _character(self, i: discord.Interaction) -> None:
        await self._cmd(i, "character")

    @guess.command(name="character_bw", description="Guess the holomem from grayscale card art.")
    async def _character_bw(self, i: discord.Interaction) -> None:
        await self._cmd(i, "character_bw")

    @guess.command(name="chart", description="Guess the song from its Expert chart.")
    async def _chart(self, i: discord.Interaction) -> None:
        await self._cmd(i, "chart")

    @guess.command(name="chart_hard", description="Guess the song from its Hard chart.")
    async def _chart_hard(self, i: discord.Interaction) -> None:
        await self._cmd(i, "chart_hard")

    @guess.command(name="notes", description="Guess the song from its note count.")
    async def _notes(self, i: discord.Interaction) -> None:
        await self._cmd(i, "notes")

    @guess.command(name="music", description="Guess the song from an audio clip.")
    async def _music(self, i: discord.Interaction) -> None:
        await self._cmd(i, "music")

    @guess.command(name="event_background", description="Guess the event from its banner.")
    async def _event_background(self, i: discord.Interaction) -> None:
        await self._cmd(i, "event_background")

    # --- meta ---

    @guess.command(name="stats", description="View guessing stats.")
    @app_commands.describe(user="Whose stats to view.")
    async def stats(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        assert self.bot.user_data
        target = user or interaction.user
        all_stats = await self.bot.user_data.get_guesses(target.id)
        succ = sum(s.get("success", 0) for s in all_stats.values())
        fail = sum(s.get("fail", 0) for s in all_stats.values())
        hints = sum(s.get("hint", 0) for s in all_stats.values())
        ended = sum(s.get("ragequit", 0) for s in all_stats.values())
        total = succ + fail
        acc = f"{succ / total * 100:.1f}%" if total else "N/A"
        embed = embeds.embed(
            title=f"{target.display_name}'s Guess Stats",
            description=(
                f"**Correct:** {succ:,}\n"
                f"**Incorrect:** {fail:,}\n"
                f"**Accuracy:** {acc}\n"
                f"**Hints used:** {hints:,}\n"
                f"**Guesses ended:** {ended:,}"
            ),
        )
        await interaction.response.send_message(embed=embed)

    @guess.command(name="leaderboard", description="Top guessers for a mode.")
    @app_commands.describe(mode="Guessing mode.")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in MODES])
    async def leaderboard(self, interaction: discord.Interaction, mode: str = "jacket") -> None:
        assert self.bot.user_data
        await interaction.response.defer(thinking=True)
        rows, total_pages = await self.bot.user_data.get_guesses_leaderboard(mode, 1)
        if not rows:
            await interaction.followup.send(
                embed=embeds.error_embed(f"No leaderboard data for **{mode}** yet.")
            )
            return
        view = _GuessLeaderboardView(self, mode, total_pages, interaction.user.id)
        embed = await view.render()
        if total_pages <= 1:
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        view.message = await interaction.followup.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none(), wait=True
        )

    # --- chat handling ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.content.startswith(GUESS_PREFIX):
            return
        if message.content.startswith("-#"):  # discord subheader markdown, not a guess
            return
        data = self.rounds.get(message.channel.id)
        if not data or not data.get("startTime"):
            return
        content = message.content[len(GUESS_PREFIX) :].strip()
        if not content:
            return
        command = "".join(content.lower().split())
        if command == "hint":
            await self._chat_hint(message, data)
            return
        if command == "end":
            await self._chat_end(message, data)
            return
        if command == "time":
            await message.reply(embed=self._time_embed(data))
            return
        data["guessers"].add(message.author.id)
        gt = data["guessType"]
        if gt == "song":
            await self._check_song(message, data, content)
        elif gt == "character":
            await self._check_character(message, data, content)
        else:
            await self._check_event(message, data, content)

    async def _fail(self, message: discord.Message, data: dict) -> None:
        if self.bot.user_data:
            await self.bot.user_data.add_guesses(message.author.id, data["guessing"], "fail")

    async def _check_song(self, message: discord.Message, data: dict, content: str) -> None:
        assert self.bot.data and self.bot.holo
        tip = MUSIC_TIP if data["guessing"] == "music" else GUESS_TIP
        hit = self.bot.data.match_song_aliased(content)
        if not hit:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find a song matching `{content}`." + tip, title="Incorrect"
                )
            )
            return
        if hit.id == data["answer"]:
            await self._award(message, data)
            return
        desc = f"Incorrectly guessed **`{hit.title}`**."
        if data["guessing"] == "notes":
            try:
                detail = await self.bot.holo.get_song(hit.id)
                exp = next(
                    (d.fullComboNoteCount for d in detail.difficulties if d.difficultyType.endswith("EXPERT")),
                    None,
                )
                if exp:
                    desc += f"\n\n**This song's Expert chart has `{exp}` notes.**"
            except Exception:
                pass
        await message.reply(embed=embeds.error_embed(desc + tip, title="Incorrect"))
        await self._fail(message, data)

    async def _check_character(self, message: discord.Message, data: dict, content: str) -> None:
        assert self.bot.data
        hit = self.bot.data.match_holomem_aliased(content)
        if not hit:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find a holomem matching `{content}`." + GUESS_TIP, title="Incorrect"
                )
            )
            return
        if hit.id == data.get("holomem_id"):
            await self._award(message, data)
        else:
            await message.reply(
                embed=embeds.error_embed(
                    f"Incorrectly guessed **{hit.name}**." + GUESS_TIP, title="Incorrect"
                )
            )
            await self._fail(message, data)

    async def _check_event(self, message: discord.Message, data: dict, content: str) -> None:
        assert self.bot.data
        try:
            events = await self.bot.data.events(data.get("region", "us"), self.bot.holo.lang)  # type: ignore[union-attr]
        except HolodoriError:
            events = []
        hit = self.bot.data.match_event_aliased(events, content)
        if not hit:
            await message.reply(
                embed=embeds.error_embed(
                    f"Couldn't find an event matching `{content}`." + GUESS_TIP, title="Incorrect"
                )
            )
            return
        if hit.eventId == data["answer"]:
            await self._award(message, data)
        else:
            wrong = embeds.error_embed(
                f"Incorrectly guessed **`{hit.name}`**." + GUESS_TIP, title="Incorrect"
            )
            logo = self.bot.holo.unsquished_image_url(hit.logo)  # type: ignore[union-attr]
            if logo:
                wrong.set_thumbnail(url=logo)
            await message.reply(embed=wrong)
            await self._fail(message, data)

    # --- hints ---

    def _end_error(self, data: dict, user_id: int) -> str | None:
        # the starter can always end; others need a prior guess and to wait out the gate
        if user_id == data["starter"]:
            return None
        if user_id not in data["guessers"]:
            return "You must make at least one guess before giving up."
        remaining = _giveup_gate(data) - (time.time() - data["startTime"])
        if remaining > 0:
            return f"Cannot end the guess for another `{round(remaining)}` seconds."
        return None

    async def _end_round(self, data: dict, ender_id: int, channel: discord.abc.Messageable) -> None:
        self.rounds.pop(getattr(channel, "id", 0), None)
        if self.bot.user_data:
            await self.bot.user_data.add_guesses(ender_id, data["guessing"], "ragequit")
        embed, files = self._reveal_embed(
            data,
            title="Guess Ended",
            description=f"The answer was **{data['answerName']}**." + self._reveal_extras(data),
            color=discord.Color.red(),
        )
        view = _GuessResultView(self, data)
        view.message = await channel.send(embed=embed, files=files, view=view)

    async def _chat_end(self, message: discord.Message, data: dict) -> None:
        err = self._end_error(data, message.author.id)
        if err:
            await message.reply(embed=embeds.error_embed(err, title="Guess Ended"))
            return
        await self._end_round(data, message.author.id, message.channel)

    async def _record_hint(self, user_id: int, data: dict, advanced: bool) -> None:
        if advanced and self.bot.user_data:
            await self.bot.user_data.add_guesses(user_id, data["guessing"], "hint")

    async def _chat_hint(self, message: discord.Message, data: dict) -> None:
        embed, files, advanced = await self._resolve_hint(data)
        await self._record_hint(message.author.id, data, advanced)
        await message.reply(embed=embed, files=files)

    @guess.command(name="hint", description="Get a hint for the active guess.")
    async def hint(self, interaction: discord.Interaction) -> None:
        data = self.rounds.get(interaction.channel_id or 0)
        if not data or not data.get("startTime"):
            await interaction.response.send_message(
                embed=embeds.error_embed("There's no active round here."), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        embed, files, advanced = await self._resolve_hint(data)
        await self._record_hint(interaction.user.id, data, advanced)
        await interaction.followup.send(embed=embed, files=files)

    @guess.command(name="end", description="End the active guess.")
    async def end(self, interaction: discord.Interaction) -> None:
        data = self.rounds.get(interaction.channel_id or 0)
        if not data or not data.get("startTime"):
            await interaction.response.send_message(
                embed=embeds.error_embed("There's no active round here."), ephemeral=True
            )
            return
        err = self._end_error(data, interaction.user.id)
        if err:
            await interaction.response.send_message(
                embed=embeds.error_embed(err, title="Guess Ended"), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        await self._end_round(data, interaction.user.id, interaction.channel)  # type: ignore[arg-type]
        await interaction.followup.send("Ended.", ephemeral=True)

    @guess.command(name="time", description="Time remaining in the active guess.")
    async def time_left(self, interaction: discord.Interaction) -> None:
        data = self.rounds.get(interaction.channel_id or 0)
        if not data or not data.get("startTime"):
            await interaction.response.send_message(
                embed=embeds.error_embed("There's no active round here."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self._time_embed(data))

    async def _resolve_hint(self, data: dict) -> tuple[discord.Embed, list[discord.File], bool]:
        if data["guessing"] == "music":
            return await self._music_hint(data)
        d = data["data"]
        stage = d.get("hint_stage", 0)
        advanced = stage < MAX_TEXT_HINTS
        if advanced:
            now = time.time()
            if now - d.get("last_hint", 0.0) < HINT_COOLDOWN:
                return embeds.error_embed("Please wait a moment before the next hint."), [], False
            d["last_hint"] = now
            stage += 1
            d["hint_stage"] = stage
        lines = self._tier_lines(data, stage)
        if not advanced:
            lines.append("-# All hints have been revealed.")
        embed = embeds.embed(
            title=f"Guess Hint - Stage {stage}/{MAX_TEXT_HINTS}",
            description="\n".join(lines),
            color=discord.Color.yellow(),
        )
        return embed, [], advanced

    def _tier_lines(self, data: dict, stage: int) -> list[str]:
        d = data["data"]
        name = str(data["answerName"])
        lines: list[str] = []
        if data["guessType"] == "song":
            song = self.bot.data.get_song(data["answer"]) if self.bot.data else None
            if stage >= 1:
                diff = CHART_DIFF.get(data["guessing"], "expert")
                level = self._diff_level(song, diff) if song else None
                if level is None:
                    lines.append(f"This song doesn't have a {diff.title()} chart.")
                else:
                    lines.append(f"The song is level **`{level}`** on **{diff.title()}**.")
            if stage >= 2:
                lines.append(f"The name has **`{len(name)}`** characters.")
            if stage >= 3:
                d.setdefault("masked", _masked_name(name, REVEAL_FRACTION))
                lines.append(f"Name: `{d['masked']}`")
        elif data["guessType"] == "character":
            if stage >= 1:
                lines.append(f"The rarity of this card is {emojis.rarity(d.get('rarity', 0)) or '?'}.")
            if stage >= 2:
                attr = d.get("attr")
                icon = emojis.attr(attr)
                lines.append(f"The attribute of this card is {(icon + ' ') if icon else ''}**{attr or 'unknown'}**.")
            if stage >= 3:
                lines.append(f"This card belongs to **{d.get('group') or 'unknown'}**.")
        else:  # event
            if stage >= 1:
                lines.append(f"This is a **{d.get('event_type', 'Marathon')}** event.")
            if stage >= 2:
                lines.append(f"The name has **`{len(name)}`** characters.")
            if stage >= 3:
                d.setdefault("masked", _masked_name(name, REVEAL_FRACTION))
                lines.append(f"Name: `{d['masked']}`")
        return lines

    async def _music_hint(self, data: dict) -> tuple[discord.Embed, list[discord.File], bool]:
        d = data["data"]
        stage = d.get("stage", 1)
        if stage >= MAX_STAGE:
            embed = embeds.embed(
                title=f"Guess Hint - Stage {MAX_STAGE}/{MAX_STAGE}",
                description="The full clip has been revealed.",
                color=discord.Color.yellow(),
            )
            files = [discord.File(io.BytesIO(d["audio"]), "clip.mp3")] if d.get("audio") else []
            return embed, files, False
        now = time.time()
        if now - d.get("last_hint", 0.0) < HINT_COOLDOWN:
            return embeds.error_embed("Please wait a moment before the next hint."), [], False
        d["last_hint"] = now
        stage += 1
        d["stage"] = stage
        clip = await song_clip.clip(d["audio_full"], d["window"], STAGE_SECONDS[stage])
        if clip:
            d["audio"] = clip
        embed = embeds.embed(
            title=f"Guess Hint - Stage {stage}/{MAX_STAGE}",
            description=f"Here's **{int(STAGE_SECONDS[stage])}s** of the song.",
            color=discord.Color.yellow(),
        )
        files = [discord.File(io.BytesIO(d["audio"]), "clip.mp3")] if d.get("audio") else []
        return embed, files, True


class _GuessLeaderboardView(HoloView):
    _PER_PAGE = 20

    def __init__(self, cog: GuessCog, mode: str, total_pages: int, restrict_to: int) -> None:
        super().__init__(timeout=180, restrict_to=restrict_to)
        self.cog = cog
        self.mode = mode
        self.total_pages = max(1, total_pages)
        self.page = 1
        self._update()

    def _update(self) -> None:
        self.prev.disabled = self.page == 1
        self.next.disabled = self.page == self.total_pages

    async def render(self) -> discord.Embed:
        assert self.cog.bot.user_data
        rows, total = await self.cog.bot.user_data.get_guesses_leaderboard(self.mode, self.page)
        self.total_pages = max(1, total)
        lines = [
            f"**#{(self.page - 1) * self._PER_PAGE + i}** <@{r['discord_id']}> - `{r['score']}`"
            for i, r in enumerate(rows, 1)
        ]
        self._update()
        embed = embeds.embed(
            title=f"Guess Leaderboard - {self.mode}",
            description="\n".join(lines) or "No data.",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Page {self.page}/{self.total_pages}")
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page > 1:
            self.page -= 1
        await interaction.response.edit_message(embed=await self.render(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page < self.total_pages:
            self.page += 1
        await interaction.response.edit_message(embed=await self.render(), view=self)


class _GuessResultView(HoloView):
    """buttons on a finished guess: play again plus type-specific info, reusing the slash commands."""

    def __init__(self, cog: GuessCog, data: dict) -> None:
        super().__init__(timeout=30)
        self.cog = cog
        self.data = data
        gt = data["guessType"]
        if gt == "song":
            self._add("Song Info", "SongCog", "info", str(data["answer"]))
        elif gt == "character":
            if data.get("card_id"):
                self._add("View Card", "CardsCog", "info", str(data["card_id"]))
            if data.get("holomem_id"):
                self._add("Holomem Info", "HolomemCog", "info", str(data["holomem_id"]))
        elif gt == "event":
            self._add("Event Info", "EventsCog", "info", data.get("region", "us"), str(data["answer"]))

    def _add(self, label: str, cog_name: str, command: str, *args: Any) -> None:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.gray)

        async def callback(interaction: discord.Interaction, _b=button) -> None:
            _b.disabled = True
            try:
                if interaction.message:
                    await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
            await self._invoke(interaction, cog_name, command, args)

        button.callback = callback  # type: ignore[method-assign]
        self.add_item(button)

    async def _invoke(
        self, interaction: discord.Interaction, cog_name: str, command: str, args: tuple
    ) -> None:
        cog = self.cog.bot.get_cog(cog_name)
        if cog is None:
            await interaction.response.send_message(
                embed=embeds.error_embed("That isn't available right now."), ephemeral=True
            )
            return
        await getattr(cog, command).callback(cog, interaction, *args)

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await self.cog._start(interaction, self.data["guessing"])


async def setup(bot: HolodoriBot) -> None:
    if bot.config.get("guessing", {}).get("enabled", True):
        await bot.add_cog(GuessCog(bot))
