from __future__ import annotations

import time
from collections import Counter
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.views import HoloView

if TYPE_CHECKING:
    from main import HolodoriBot


def _text_commands_embed() -> discord.Embed:
    # the %-prefix commands, listed for the /help "Text Commands" button. region + tier are accepted
    # in any order; regions are fuzzy (US/AS/JP), tiers are 1 / t100 / 500 / ...
    return embeds.embed(
        title="Text Commands",
        description=(
            "These also work with a `%` prefix. Region and tier can be given in any order.\n\n"
            "**%cutoff** `[region] {tier}` - Cutoff points, speed, and the projected final for a tier.\n"
            "**%graph** / **%eph** `[region] {tier}` - Graph a tier's cutoff over the event.\n"
            "**%heatmap** `[region] {tier}` - Hourly event-point gain (EPH) heatmap.\n"
            "**%leaderboard** / **%lb** `[region]` - Current event's top 100.\n"
            "**%player** `{name}` - Look up a player by name across every region's top 100.\n\n"
            "-# Regions: US / AS / JP (fuzzy). Tiers: `1`, `t100`, `500`, ..."
        ),
    )


class _HelpView(HoloView):
    """the /help message's buttons. anyone may click; each gets an ephemeral reply."""

    def __init__(self) -> None:
        super().__init__(timeout=600)  # no restrict_to -> anyone can open the list

    @discord.ui.button(label="Text Commands", style=discord.ButtonStyle.secondary)
    async def text_commands(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(embed=_text_commands_embed(), ephemeral=True)


class InfoCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    def _trim_cmd_log(self) -> None:
        cutoff = time.time() - 60
        log = self.bot.cache.executed_commands
        while log and log[0][1] < cutoff:
            log.popleft()

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: app_commands.Command
    ) -> None:
        self.bot.cache.executed_commands.append(
            (command.qualified_name, time.time(), interaction.user.id)
        )
        self._trim_cmd_log()

    @app_commands.command(name="ping", description="Check the bot's latency and recent activity.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        self._trim_cmd_log()
        log = self.bot.cache.executed_commands
        cmds_ran = len(log) + 1
        users = {interaction.user.id} | {uid for _, _, uid in log}
        counter = Counter(cmd for cmd, _, _ in log)
        counter["ping"] += 1
        popular = (
            f"`/{counter.most_common(1)[0][0]}` was the most popular command in the last minute."
            if counter
            else "No commands were ran."
        )
        embed = embeds.embed(
            title="Pong!",
            description=(
                f"**Latency:** `{round(self.bot.latency * 1000, 2)}`ms\n\n"
                f"**{cmds_ran:,}** commands ran in the last minute.\n"
                f"**{len(users)}** users ran commands in the last minute.\n{popular}"
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="help", description="Bot info and links.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.user
        cfg = self.bot.config["discord"]
        links = [
            f"**Invite:** https://discord.com/oauth2/authorize?client_id={self.bot.user.id}"
        ]
        for label, key in (
            ("Support", "support_invite"),
            ("Terms of Service", "tos_url"),
            ("Privacy Policy", "privacy_url"),
        ):
            url = str(cfg.get(key) or "").strip()
            if url:
                links.append(f"**{label}:** {url}")
        embed = embeds.embed(
            title=self.bot.user.name,
            description=(
                "\n".join(links) + "\n\n"
                "-# This is an unofficial, fan-made bot and is not affiliated with or endorsed by "
                "QualiArts Inc., COVER Corp., hololive Dreams, or any hololive members."
            ),
        )
        view = _HelpView()
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    @app_commands.command(name="donate", description="Support the bot.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def donate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        url = str(self.bot.config["discord"].get("donate_url") or "").strip()
        embed = embeds.embed(
            title="Donations",
            description=(
                "Donations are strictly **optional** and help cover hosting costs.\n\n"
                + (f"**LINK:** {url}" if url else "")
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(InfoCog(bot))
