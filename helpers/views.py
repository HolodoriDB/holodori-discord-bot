from __future__ import annotations

from typing import Callable

import discord


class HoloView(discord.ui.View):
    """base view: tracks its message and disables itself on timeout."""

    def __init__(self, *, timeout: float | None = 180, restrict_to: int | None = None) -> None:
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None
        self.restrict_to = restrict_to
        self._prev_enabled: list[discord.ui.Button | discord.ui.Select] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.restrict_to is not None and interaction.user.id != self.restrict_to:
            await interaction.response.send_message(
                "You can't interact with this — run the command yourself.", ephemeral=True
            )
            return False
        return True

    def _disable_all(self) -> None:
        self._prev_enabled = [
            item
            for item in self.children
            if isinstance(item, (discord.ui.Button, discord.ui.Select)) and not item.disabled
        ]
        for item in self._prev_enabled:
            item.disabled = True

    def _enable_all(self) -> None:
        for item in self._prev_enabled:
            item.disabled = False
        self._prev_enabled = []

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class HoloLayoutView(discord.ui.LayoutView):
    """components-v2 base (LayoutView): same restrict_to gate + message tracking + disable-on-timeout
    as HoloView, for cards whose buttons live inside a Container instead of under the message."""

    def __init__(self, *, timeout: float | None = 180, restrict_to: int | None = None) -> None:
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None
        self.restrict_to = restrict_to

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.restrict_to is not None and interaction.user.id != self.restrict_to:
            await interaction.response.send_message(
                "You can't interact with this — run the command yourself.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        changed = False
        for item in self.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select)) and not item.disabled:
                item.disabled = True
                changed = True
        if changed and self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LinkButtonView(HoloView):
    """non-expiring view holding one or more link buttons."""

    def __init__(self, buttons: list[tuple[str, str]]) -> None:
        super().__init__(timeout=None)
        for label, url in buttons:
            self.add_item(discord.ui.Button(label=label, url=url))


class Paginator(HoloView):
    """generic prev/next paginator driven by a render(page) -> Embed callable."""

    def __init__(
        self,
        render: Callable[[int], discord.Embed],
        total_pages: int,
        restriction_id: int,
        *,
        timeout: float = 180,
    ) -> None:
        super().__init__(timeout=timeout, restrict_to=restriction_id)
        self.render = render
        self.total_pages = max(1, total_pages)
        self.current_page = 1
        self._update()

    def _update(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        self._update()
        await interaction.response.edit_message(embed=self.render(self.current_page), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        self._update()
        await interaction.response.edit_message(embed=self.render(self.current_page), view=self)
