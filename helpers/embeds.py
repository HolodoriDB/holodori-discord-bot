import discord

from helpers.config_loader import get_config


class HoloEmbed(discord.Embed):
    def set_footer(self, *, text: str | None = None, icon_url: str | None = None):
        name = get_config()["discord"]["name"]
        if text:
            return super().set_footer(text=f"{name} - " + (text or ""), icon_url=icon_url)
        return super().set_footer(text=name, icon_url=icon_url)


def embed(*args, **kwargs) -> HoloEmbed:
    if len(args) == 1:
        kwargs["description"] = args[0]
        args = ()
    em = HoloEmbed(*args, **kwargs)
    em.timestamp = discord.utils.utcnow()
    em.set_footer(text="")
    return em


def error_embed(
    description: str, title: str | None = None, color: discord.Color | None = None
) -> HoloEmbed:
    return embed(
        title="❌ Error" if not title else f"❌ {title}",
        description=description,
        color=color or discord.Color.red(),
    )


def success_embed(
    description: str, title: str | None = None, color: discord.Color | None = None
) -> HoloEmbed:
    return embed(
        title="✅ Success" if not title else f"✅ {title}",
        description=description,
        color=color or discord.Color.green(),
    )


def warn_embed(
    description: str, title: str | None = None, color: discord.Color | None = None
) -> HoloEmbed:
    return embed(
        title="⚠️ Warning" if not title else f"⚠️ {title}",
        description=description,
        color=color or discord.Color.orange(),
    )
