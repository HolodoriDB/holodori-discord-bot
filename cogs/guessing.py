from __future__ import annotations

import asyncio
import io
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import embeds, imaging
from services import song_clip
from services.holodori import HolodoriError

if TYPE_CHECKING:
    from main import HolodoriBot

ROUND_SECONDS = 35
DEFAULT_REGION = "us"

# mode -> (kind, prompt). kind drives answer matching + reveal art.
MODES: dict[str, tuple[str, str]] = {
    "jacket": ("song", "Guess the song from a piece of its jacket!"),
    "jacket_30px": ("song", "Guess the song from its pixelated jacket!"),
    "jacket_bw": ("song", "Guess the song from a grayscale piece of its jacket!"),
    "jacket_challenge": ("song", "Guess the song from a tiny piece of its jacket!"),
    "character": ("character", "Guess the holomem from a piece of card art!"),
    "character_bw": ("character", "Guess the holomem from grayscale card art!"),
    "chart": ("song", "Guess the song from a slice of its EXPERT chart!"),
    "chart_hard": ("song", "Guess the song from a slice of its HARD chart!"),
    "notes": ("song", "Guess the song from its note count!"),
    "music": ("song", "Guess the song from a short audio clip!"),
    "event_background": ("event", "Guess the event from a piece of its banner!"),
}


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass
class Round:
    mode: str
    kind: str
    answers: set[str]
    display_answer: str
    starter_id: int
    started: float
    hints: list[str]
    reveal_image: str | None = None
    image: bytes | None = None
    hint_level: int = 0
    solved: bool = False
    message: discord.Message | None = None
    audio: bytes | None = None


class GuessCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot
        self.rounds: dict[int, Round] = bot.cache.guess_channels
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

    # --- round construction ---

    async def _pick_song_bytes(self, url_for) -> tuple:
        """try up to 5 random songs until one's asset fetches; returns (song, bytes)."""
        assert self.bot.data and self.bot.holo
        songs = list(self.bot.data.songs())
        random.shuffle(songs)
        for s in songs[:8]:
            url = url_for(s)
            if not url:
                continue
            try:
                data = await self.bot.holo.fetch_bytes(url)
                return s, data
            except Exception:
                continue
        return None, None

    async def _build(self, mode: str) -> Round | None:
        assert self.bot.data and self.bot.holo
        kind = MODES[mode][0]
        if kind == "song":
            return await self._build_song(mode)
        if kind == "character":
            return await self._build_character(mode)
        return await self._build_event(mode)

    def _song_answers(self, song) -> set[str]:
        return {normalize(song.title)}

    async def _build_song(self, mode: str) -> Round | None:
        assert self.bot.data and self.bot.holo
        holo = self.bot.holo

        if mode in ("chart", "chart_hard"):
            diff = "expert" if mode == "chart" else "hard"
            song, data = await self._pick_song_bytes(lambda s: holo.chart_image_url(s.id, diff))
            if not song:
                return None
            img = await asyncio.to_thread(imaging.crop_chart_strip, data)
            return self._song_round(mode, song, image=img)

        if mode == "notes":
            songs = list(self.bot.data.songs())
            random.shuffle(songs)
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
                    r.hints.insert(0, f"The EXPERT chart has **{expert.fullComboNoteCount}** notes.")
                    return r
            return None

        if mode == "music":
            if not song_clip.HAS_FFMPEG:
                return None
            song, mp3 = await self._pick_song_bytes(
                lambda s: f"{holo.base}/api/songs/{s.id}/audio"
            )
            if not song or not mp3:
                return None
            start = random.uniform(5, max(6, (song.length or 30) - 10))
            clip = await song_clip.clip(mp3, start, 3.0)
            if not clip:
                return None
            r = self._song_round(mode, song, image=None)
            r.audio = clip
            return r

        # jacket family
        song, data = await self._pick_song_bytes(lambda s: holo.image_url(s.jacket))
        if not song:
            return None
        if mode == "jacket_30px":
            img = await asyncio.to_thread(imaging.pixelate, data, px=30)
        elif mode == "jacket_bw":
            img = await asyncio.to_thread(imaging.crop_square, data, grayscale=True)
        elif mode == "jacket_challenge":
            img = await asyncio.to_thread(imaging.crop_square, data, frac=0.22)
        else:
            img = await asyncio.to_thread(imaging.crop_square, data)
        return self._song_round(mode, song, image=img)

    def _song_round(self, mode: str, song, *, image: bytes | None) -> Round:
        assert self.bot.holo
        hints = [
            f"Category: **{(song.category or 'unknown').title()}**",
            f"Starts with **{song.title[0]}**",
            f"**{len(song.title)}** characters long",
        ]
        r = Round(
            mode=mode,
            kind="song",
            answers=self._song_answers(song),
            display_answer=song.title,
            starter_id=0,
            started=time.time(),
            hints=hints,
            reveal_image=self.bot.holo.image_url(song.jacket),
            image=image,
        )
        return r

    async def _build_character(self, mode: str) -> Round | None:
        assert self.bot.data and self.bot.holo
        holo = self.bot.holo
        cards = [c for c in self.bot.data.cards() if c.image and c.rarity >= 3]
        random.shuffle(cards)
        for card in cards[:8]:
            url = holo.image_url(card.image)
            if not url:
                continue
            try:
                data = await holo.fetch_bytes(url)
            except Exception:
                continue
            answers = {normalize(card.character)}
            member = self.bot.data.get_holomem(card.characterId)
            if member:
                answers.add(normalize(member.name))
                if member.shortName:
                    answers.add(normalize(member.shortName))
            img = await asyncio.to_thread(
                imaging.crop_square, data, grayscale=(mode == "character_bw")
            )
            hints = [
                f"From card **{card.name}**",
                f"Starts with **{card.character[0]}**",
            ]
            r = Round(
                mode=mode,
                kind="character",
                answers=answers,
                display_answer=card.character,
                starter_id=0,
                started=time.time(),
                hints=hints,
                reveal_image=holo.image_url(card.image),
                image=img,
            )
            return r
        return None

    async def _build_event(self, mode: str) -> Round | None:
        assert self.bot.data and self.bot.holo
        holo = self.bot.holo
        try:
            events = await self.bot.data.events(DEFAULT_REGION, holo.lang)
        except HolodoriError:
            return None
        events = [e for e in events if e.banner]
        random.shuffle(events)
        for ev in events[:6]:
            url = holo.image_url(ev.banner)
            if not url:
                continue
            try:
                data = await holo.fetch_bytes(url)
            except Exception:
                continue
            img = await asyncio.to_thread(imaging.crop_square, data, frac=0.5)
            hints = [f"Starts with **{ev.name[0]}**", f"**{len(ev.name)}** characters long"]
            r = Round(
                mode=mode,
                kind="event",
                answers={normalize(ev.name)},
                display_answer=ev.name,
                starter_id=0,
                started=time.time(),
                hints=hints,
                reveal_image=holo.image_url(ev.logo),
                image=img,
            )
            return r
        return None

    # --- start / finish ---

    async def _start(self, interaction: discord.Interaction, mode: str) -> None:
        channel_id = interaction.channel_id
        if channel_id is None:
            return
        if interaction.guild and self.bot.user_data:
            if not await self.bot.user_data.guessing_enabled(interaction.guild.id):
                await interaction.response.send_message(
                    embed=embeds.error_embed("Guessing is disabled in this server."),
                    ephemeral=True,
                )
                return
        if channel_id in self.rounds:
            await interaction.response.send_message(
                embed=embeds.error_embed("A round is already active in this channel."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            rnd = await self._build(mode)
        except Exception:
            rnd = None
        if rnd is None:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "Couldn't start that round (asset unavailable). Try again."
                )
            )
            return
        rnd.starter_id = interaction.user.id
        rnd.started = time.time()
        self.rounds[channel_id] = rnd

        embed = embeds.embed(
            title=f"Guess the {rnd.kind}!",
            description=f"{MODES[mode][1]}\nType `-<your guess>` to answer. `-hint`, `-end`, `-time`.",
            color=discord.Color.blurple(),
        )
        files: list[discord.File] = []
        image = rnd.image
        if image:
            embed.set_image(url="attachment://guess.png")
            files.append(discord.File(io.BytesIO(image), "guess.png"))
        if rnd.audio:
            files.append(discord.File(io.BytesIO(rnd.audio), "clip.mp3"))
        rnd.message = await interaction.followup.send(embed=embed, files=files, wait=True)

    async def _finish(self, rnd: Round, channel: discord.abc.Messageable, title: str) -> None:
        self.rounds.pop(_channel_id(channel), None)
        embed = embeds.embed(
            title=title,
            description=f"The answer was **{rnd.display_answer}**.",
            color=discord.Color.green() if rnd.solved else discord.Color.red(),
        )
        if rnd.reveal_image:
            embed.set_image(url=rnd.reveal_image)
        await channel.send(embed=embed)

    # --- mode commands (thin wrappers) ---

    @guess.command(name="jacket", description="Guess the song from its jacket.")
    async def _jacket(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "jacket")

    @guess.command(name="jacket_30px", description="Guess the song from a pixelated jacket.")
    async def _jacket_30px(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "jacket_30px")

    @guess.command(name="jacket_bw", description="Guess the song from a grayscale jacket.")
    async def _jacket_bw(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "jacket_bw")

    @guess.command(name="jacket_challenge", description="Guess the song from a tiny jacket crop.")
    async def _jacket_challenge(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "jacket_challenge")

    @guess.command(name="character", description="Guess the holomem from card art.")
    async def _character(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "character")

    @guess.command(name="character_bw", description="Guess the holomem from grayscale card art.")
    async def _character_bw(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "character_bw")

    @guess.command(name="chart", description="Guess the song from its EXPERT chart.")
    async def _chart(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "chart")

    @guess.command(name="chart_hard", description="Guess the song from its HARD chart.")
    async def _chart_hard(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "chart_hard")

    @guess.command(name="notes", description="Guess the song from its note count.")
    async def _notes(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "notes")

    @guess.command(name="music", description="Guess the song from an audio clip.")
    async def _music(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "music")

    @guess.command(name="event_background", description="Guess the event from its banner.")
    async def _event_background(self, interaction: discord.Interaction) -> None:
        await self._start(interaction, "event_background")

    # --- meta commands ---

    @guess.command(name="stats", description="View guessing stats.")
    @app_commands.describe(user="Whose stats to view.")
    async def stats(
        self, interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        assert self.bot.user_data
        target = user or interaction.user
        all_stats = await self.bot.user_data.get_guesses(target.id)
        if not all_stats:
            await interaction.response.send_message(
                embed=embeds.embed(
                    title=f"{target.display_name}'s Guessing Stats",
                    description="No guesses yet!",
                )
            )
            return
        lines = []
        for mode, st in sorted(all_stats.items()):
            succ, fail = st.get("success", 0), st.get("fail", 0)
            total = succ + fail
            acc = f"{succ / total * 100:.0f}%" if total else "—"
            lines.append(f"**{mode}** — {succ}✅ / {fail}❌ ({acc})")
        await interaction.response.send_message(
            embed=embeds.embed(
                title=f"{target.display_name}'s Guessing Stats", description="\n".join(lines)
            )
        )

    @guess.command(name="leaderboard", description="Top guessers for a mode.")
    @app_commands.describe(mode="Guessing mode.")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in MODES])
    async def leaderboard(self, interaction: discord.Interaction, mode: str) -> None:
        assert self.bot.user_data
        rows, _ = await self.bot.user_data.get_guesses_leaderboard(mode, 1)
        if not rows:
            await interaction.response.send_message(
                embed=embeds.error_embed(f"No one has played **{mode}** yet.")
            )
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            lines.append(f"**#{i}** <@{row['discord_id']}> — {row['score']}")
        await interaction.response.send_message(
            embed=embeds.embed(title=f"{mode} Leaderboard", description="\n".join(lines)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- chat handling ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.content.startswith("-"):
            return
        channel_id = message.channel.id
        rnd = self.rounds.get(channel_id)
        if rnd is None:
            return
        body = message.content[1:].strip()
        low = body.lower()
        if low in ("end", "giveup", "give up"):
            if message.author.id != rnd.starter_id and time.time() - rnd.started < 10:
                return
            await self._finish(rnd, message.channel, "Round ended")
            return
        if low == "time":
            left = max(0, int(ROUND_SECONDS - (time.time() - rnd.started)))
            await message.channel.send(f"⏱️ **{left}s** left.")
            return
        if low == "hint":
            await self._give_hint(rnd, message)
            return
        await self._check_guess(rnd, message, body)

    async def _give_hint(self, rnd: Round, message: discord.Message) -> None:
        assert self.bot.user_data
        if rnd.hint_level >= len(rnd.hints):
            await message.channel.send("No more hints!")
            return
        hint = rnd.hints[rnd.hint_level]
        rnd.hint_level += 1
        await self.bot.user_data.add_guesses(message.author.id, rnd.mode, "hint")
        await message.channel.send(embed=embeds.embed(title="💡 Hint", description=hint))

    async def _check_guess(self, rnd: Round, message: discord.Message, body: str) -> None:
        assert self.bot.user_data
        if normalize(body) in rnd.answers:
            rnd.solved = True
            await self.bot.user_data.add_guesses(message.author.id, rnd.mode, "success")
            await message.add_reaction("✅")
            await self._finish(
                rnd, message.channel, f"{message.author.display_name} got it!"
            )
        else:
            await self.bot.user_data.add_guesses(message.author.id, rnd.mode, "fail")
            await message.add_reaction("❌")

    # --- timeout loop ---

    @tasks.loop(seconds=2)
    async def timeout_task(self) -> None:
        now = time.time()
        for channel_id, rnd in list(self.rounds.items()):
            if rnd.solved or now - rnd.started < ROUND_SECONDS:
                continue
            channel = self.bot.get_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await self._finish(rnd, channel, "Time's up!")
                except discord.HTTPException:
                    self.rounds.pop(channel_id, None)
            else:
                self.rounds.pop(channel_id, None)

    @timeout_task.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


def _channel_id(channel: discord.abc.Messageable) -> int:
    return getattr(channel, "id", 0)


async def setup(bot: HolodoriBot) -> None:
    if bot.config.get("guessing", {}).get("enabled", True):
        await bot.add_cog(GuessCog(bot))
