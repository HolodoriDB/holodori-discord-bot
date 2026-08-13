from __future__ import annotations

import io
import textwrap
import traceback
from contextlib import redirect_stdout
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from helpers import embeds

if TYPE_CHECKING:
    from main import HolodoriBot


class DevCog(commands.Cog):
    def __init__(self, bot: HolodoriBot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context, guild_id: int | None = None) -> None:
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.bot.tree.copy_global_to(guild=guild)
            synced = await self.bot.tree.sync(guild=guild)
        else:
            synced = await self.bot.tree.sync()
        await ctx.reply(f"synced {len(synced)} commands.")

    @commands.command(name="reload")
    async def reload(self, ctx: commands.Context, cog: str) -> None:
        await self.bot.reload_extension(f"cogs.{cog}")
        await ctx.reply(f"reloaded `{cog}`.")

    @commands.command(name="load")
    async def load(self, ctx: commands.Context, cog: str) -> None:
        await self.bot.load_extension(f"cogs.{cog}")
        await ctx.reply(f"loaded `{cog}`.")

    @commands.command(name="unload")
    async def unload(self, ctx: commands.Context, cog: str) -> None:
        await self.bot.unload_extension(f"cogs.{cog}")
        await ctx.reply(f"unloaded `{cog}`.")

    @commands.command(name="refresh")
    async def refresh(self, ctx: commands.Context) -> None:
        assert self.bot.data
        await self.bot.data.refresh()
        await ctx.reply("refreshed holodori data.")

    @commands.command(name="ban")
    async def ban(self, ctx: commands.Context, user: discord.User) -> None:
        assert self.bot.user_data
        await self.bot.user_data.set_banned(user.id, True)
        self.bot.cache.discord_bans[user.id] = True
        await ctx.reply(f"banned {user.mention}.")

    @commands.command(name="unban")
    async def unban(self, ctx: commands.Context, user: discord.User) -> None:
        assert self.bot.user_data
        await self.bot.user_data.set_banned(user.id, False)
        self.bot.cache.discord_bans[user.id] = False
        await ctx.reply(f"unbanned {user.mention}.")

    @commands.command(name="eval")
    async def _eval(self, ctx: commands.Context, *, body: str) -> None:
        body = body.strip("` \n")
        if body.startswith("py"):
            body = body[2:]
        env = {"bot": self.bot, "ctx": ctx, "discord": discord}
        stdout = io.StringIO()
        wrapped = "async def __ev():\n" + textwrap.indent(body, "    ")
        try:
            exec(wrapped, env)  # owner-only debug eval
            with redirect_stdout(stdout):
                ret = await env["__ev"]()
        except Exception:
            await ctx.reply(embed=embeds.error_embed(f"```py\n{traceback.format_exc()[:1800]}```"))
            return
        out = stdout.getvalue()
        text = f"{out}{ret if ret is not None else ''}".strip() or "done."
        await ctx.reply(f"```py\n{text[:1900]}```")


async def setup(bot: HolodoriBot) -> None:
    await bot.add_cog(DevCog(bot))
