from __future__ import annotations

import discord

PANEL_TIMEOUT = 30.0


class OwnerPanelView(discord.ui.View):
    def __init__(self, *, owner_id: int, timeout: float = PANEL_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.owner_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This panel isn't yours.", ephemeral=True)
        else:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
