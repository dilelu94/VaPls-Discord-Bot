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

        target_msg = self.message
        target_interaction = self.bound_interaction

        if isinstance(target_msg, discord.Interaction):
            if target_interaction is None:
                target_interaction = target_msg
            target_msg = None

        edited = False
        if target_msg is not None and hasattr(target_msg, "edit"):
            try:
                await target_msg.edit(view=None)
                edited = True
            except Exception as e:
                logger.warning("Failed to edit message on_timeout in %s: %s", self.__class__.__name__, e)

        if not edited and target_interaction is not None:
            try:
                if target_interaction.response.is_done():
                    await target_interaction.edit_original_response(view=None)
                    edited = True
                else:
                    await target_interaction.response.edit_message(view=None)
                    edited = True
            except Exception as e:
                logger.warning("Failed to edit interaction on_timeout in %s: %s", self.__class__.__name__, e)

        if not edited:
            logger.debug("on_timeout in %s ran without a bound message or interaction reference", self.__class__.__name__)

        self.stop()

    async def on_timeout_extra(self) -> None:
        """Hook for subclasses to perform custom cleanup before buttons are removed."""
        pass
