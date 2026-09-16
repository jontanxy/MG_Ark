"""Authorisation decorators, actor resolution and the per-user "pending prompt" state."""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..config import Settings
from ..constants import ROLE_LABELS, ROLE_RANK, Role, UserStatus
from ..db import session_scope
from ..models import User
from ..services import groups as group_service
from ..services import users as user_service
from ..services.drive import DriveClient
from .ratelimit import Breaker, SlidingWindow

log = logging.getLogger(__name__)

Scope = Literal["private", "group", "any"]
Handler = Callable[..., Awaitable[Any]]

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


# ----------------------------------------------------------------------------------------
# bot_data accessors
# ----------------------------------------------------------------------------------------


def settings_of(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def drive_of(context: ContextTypes.DEFAULT_TYPE) -> DriveClient:
    return context.bot_data["drive"]


def project_lock(context: ContextTypes.DEFAULT_TYPE, project_id: int) -> asyncio.Lock:
    locks: dict[int, asyncio.Lock] = context.bot_data.setdefault("project_locks", {})
    return locks.setdefault(project_id, asyncio.Lock())


def limiter(context: ContextTypes.DEFAULT_TYPE, name: str, limit: int, window_seconds: float) -> SlidingWindow:
    limiters: dict[str, SlidingWindow] = context.bot_data.setdefault("limiters", {})
    if name not in limiters:
        limiters[name] = SlidingWindow(limit, window_seconds)
    return limiters[name]


def password_breaker(context: ContextTypes.DEFAULT_TYPE) -> Breaker:
    breaker = context.bot_data.get("password_breaker")
    if breaker is None:
        s = settings_of(context)
        breaker = Breaker(s.password_breaker_failures, s.password_breaker_window_minutes * 60, s.password_breaker_pause_minutes * 60)
        context.bot_data["password_breaker"] = breaker
    return breaker


def may_reply_to_unregistered(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Unknown senders get at most one reply per interval, so the bot cannot be used as a reply amplifier."""
    interval = settings_of(context).unregistered_reply_interval_seconds
    return limiter(context, "unregistered", 1, interval).allow(user_id)


# ----------------------------------------------------------------------------------------
# Pending prompt (one per user): replaces ConversationHandler for free-text steps.
# ----------------------------------------------------------------------------------------

PROMPT_KEY = "prompt"
PROMPT_TTL_SECONDS = 30 * 60


def set_prompt(context: ContextTypes.DEFAULT_TYPE, kind: str, **data: Any) -> None:
    context.user_data[PROMPT_KEY] = {"kind": kind, "created_at": time.monotonic(), **data}


def get_prompt(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    """The pending prompt, or None. Stale prompts (older than PROMPT_TTL_SECONDS) are dropped."""
    prompt = context.user_data.get(PROMPT_KEY)
    if prompt is None:
        return None
    if time.monotonic() - float(prompt.get("created_at", 0)) > PROMPT_TTL_SECONDS:
        context.user_data.pop(PROMPT_KEY, None)
        return {"kind": "expired", "expired_kind": prompt.get("kind")}
    return prompt


def clear_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PROMPT_KEY, None)


# ----------------------------------------------------------------------------------------
# Actor resolution
# ----------------------------------------------------------------------------------------


def resolve_actor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
    """Load (and keep fresh) the DB user for the Telegram user behind this update."""
    tg_user = update.effective_user
    if tg_user is None or tg_user.is_bot:
        return None
    settings = settings_of(context)
    with session_scope() as session:
        if tg_user.id == settings.super_admin_telegram_id:
            return user_service.ensure_super_admin(session, tg_user.id, tg_user.full_name, tg_user.username)
        user = user_service.get_user(session, tg_user.id)
        if user is not None:
            user_service.touch_profile(user, tg_user.full_name, tg_user.username)
        return user


def is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == ChatType.PRIVATE


def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in GROUP_TYPES


async def deny(update: Update, text: str, *, alert: bool = True) -> None:
    """Tell the user why an action was refused (alert for button presses, reply for messages)."""
    query = update.callback_query
    try:
        if query is not None:
            await query.answer(text[:200], show_alert=alert)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(text)
    except BadRequest as exc:  # pragma: no cover - network edge
        log.debug("deny() failed: %s", exc)


def require(
    min_role: Role | None = None,
    scope: Scope = "private",
    *,
    group_auth: bool = True,
    clears_prompt: bool = True,
) -> Callable[[Handler], Handler]:
    """Guard a handler.

    * ``scope`` restricts where the handler may run (private chat, MG group, anywhere).
    * The Telegram user must be a registered, ACTIVE user with at least ``min_role``.
    * In groups the chat must additionally be an ACTIVE MG Group (unless ``group_auth=False``).
    The resolved :class:`User` is passed to the handler as the ``actor`` keyword argument.
    """

    def decorator(func: Handler) -> Handler:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any) -> Any:
            private, group = is_private(update), is_group(update)
            if scope == "private" and not private:
                if group:
                    await deny(update, "That only works in a private chat with me — open a direct message and try again.")
                return None
            if scope == "group" and not group:
                await deny(update, "That command only works inside an MG Group.")
                return None
            if not private and not group:
                return None
            actor = resolve_actor(update, context)
            if actor is None:
                if update.callback_query is None and not may_reply_to_unregistered(context, update.effective_user.id):
                    return None  # throttled: stay silent
                if private:
                    await deny(update, "You're not registered yet. Send /start to enter the access password.")
                else:
                    await deny(update, "You're not an authorised user of this bot. Message me privately and send /start.")
                return None
            if actor.status != UserStatus.ACTIVE:
                await deny(update, "Your access has been revoked. Contact the Super Admin.")
                return None
            if group and group_auth:
                with session_scope() as session:
                    authorised = group_service.is_group_authorised(session, update.effective_chat.id)
                if not authorised:
                    await deny(update, "This group is not an authorised MG Group. A Team Lead can authorise it with /activate <token>.")
                    return None
            if min_role is not None and ROLE_RANK[actor.role] < ROLE_RANK[min_role]:
                await deny(update, f"This action requires the {ROLE_LABELS[min_role]} role.")
                return None
            # Any command or button press cancels a pending free-text step, so later text is never
            # misread as a metadata value / new password.
            if clears_prompt and (
                update.callback_query is not None
                or (update.message is not None and update.message.text and update.message.text.startswith("/"))
            ):
                clear_prompt(context)
            return await func(update, context, *args, actor=actor, **kwargs)

        return wrapper

    return decorator


async def safe_edit(update: Update, text: str, reply_markup=None) -> None:
    """Edit the callback message, tolerating 'message is not modified' and falling back to a new message."""
    query = update.callback_query
    if query is None or query.message is None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(text, reply_markup=reply_markup)
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        msg = str(exc).lower()
        if "not modified" in msg:
            return
        if "no text in the message" in msg or "message can't be edited" in msg or "message to edit not found" in msg:
            await query.message.reply_text(text, reply_markup=reply_markup)
            return
        raise
