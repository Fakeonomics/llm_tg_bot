"""Marks users seen when they message the bot in private.

Covers /start and any PM activity (one place, no per-handler edits).
Group activity does NOT count: raising the inline tier requires STARTING
the bot (opening PM), which is the referral goal.
"""
from typing import Any, Awaitable, Callable


class SeenMiddleware:
    async def __call__(
        self,
        handler: Callable,
        event: Any,
        data: dict,
    ) -> Awaitable[Any]:
        try:
            chat = getattr(event, "chat", None)
            user = getattr(event, "from_user", None)
            if (chat is not None and user is not None
                    and getattr(chat, "type", "") == "private"):
                from src.services import inline_limits as lim
                lim.mark_seen(user.id)
        except Exception:
            pass
        return await handler(event, data)
