"""Base UI View module for VaPls-Discord-Bot.

Provides ``BaseView``, the base class for all interactive UI views in the bot.
When a ``BaseView`` expires (times out), its ``on_timeout()`` method automatically
strips all buttons/components from the Discord message by editing the message with
``view=None``. Subclasses can override ``on_timeout_extra()`` for custom cleanup.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

logger = logging.getLogger("bot.baseView")


class BaseView(discord.ui.View):
    """Base View class that automatically removes all buttons/components on timeout.

    Attributes:
        message: Optional reference to the sent ``discord.Message``.
        bound_interaction: Optional reference to the invoking ``discord.Interaction``.
    """

    def __init__(self, timeout: Optional[float] = 180):
        """Initialize the base view.

        Args:
            timeout: Expiration timeout in seconds, or None for persistent views.
        """
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None
        self.bound_interaction: Optional[discord.Interaction] = None

    async def on_timeout(self) -> None:
        """Executed automatically when the view times out.

        Executes ``on_timeout_extra()``, clears internal items, edits the attached
        message/interaction with ``view=None`` to remove all buttons/components,
        and stops the view.
        """
        try:
            await self.on_timeout_extra()
        except Exception:
            logger.exception("Error executing on_timeout_extra in %s", self.__class__.__name__)

        self.clear_items()

        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass
        elif self.bound_interaction is not None:
            try:
                if self.bound_interaction.response.is_done():
                    await self.bound_interaction.edit_original_response(view=None)
                else:
                    await self.bound_interaction.response.edit_message(view=None)
            except Exception:
                pass

        self.stop()

    async def on_timeout_extra(self) -> None:
        """Hook for subclasses to perform custom cleanup before buttons are removed."""
        pass
