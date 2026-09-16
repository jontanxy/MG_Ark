from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ...constants import ROLE_LABELS, UserStatus
from ...db import session_scope
from ...services import users as user_service
from ...util import esc
from ..access import clear_prompt, is_private, may_reply_to_unregistered, password_breaker, resolve_actor, set_prompt, settings_of
from ..actions import notify_user
from .common import help_text

log = logging.getLogger(__name__)

PASSWORD_PROMPT = "🔐 <b>Enter access password</b>\n\nThis bot is private to the Motion Graphics ministry. Send the access password to register."


def _minutes(seconds: int) -> str:
    m = max(1, (seconds + 59) // 60)
    return f"{m} minute{'s' if m != 1 else ''}"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        return
    actor = resolve_actor(update, context)
    if actor is not None:
        clear_prompt(context)
        if actor.status != UserStatus.ACTIVE:
            await update.message.reply_text("🚫 Your access has been revoked. Contact the Super Admin.")
            return
        await update.message.reply_text(
            f"👋 Welcome back, <b>{esc(actor.display_name)}</b> ({ROLE_LABELS[actor.role]}).\n\n" + help_text(actor, private=True)
        )
        return
    if not may_reply_to_unregistered(context, update.effective_user.id):
        return
    with session_scope() as session:
        lock = user_service.lock_remaining_seconds(session, update.effective_user.id)
    if lock:
        await update.message.reply_text(f"⏳ Too many failed attempts. Try again in {_minutes(lock)}.")
        return
    breaker = password_breaker(context)
    if breaker.is_open():
        await update.message.reply_text(f"⏳ Registration is temporarily paused. Try again in {_minutes(breaker.remaining_seconds())}.")
        return
    set_prompt(context, "password")
    await update.message.reply_text(PASSWORD_PROMPT)


async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text received while the 'password' prompt is active."""
    settings = settings_of(context)
    tg_user = update.effective_user
    password = (update.message.text or "").strip()
    try:
        await update.message.delete()  # keep the password out of the chat history (best effort)
    except TelegramError:
        pass
    breaker = password_breaker(context)
    if breaker.is_open():
        clear_prompt(context)
        await context.bot.send_message(tg_user.id, f"⏳ Registration is temporarily paused. Try again in {_minutes(breaker.remaining_seconds())}.")
        return
    # All DB work happens before any Telegram call so no write transaction is held across an await.
    registered = None
    failures = lock_seconds = 0
    with session_scope() as session:
        existing = user_service.get_user(session, tg_user.id)
        if existing is not None and existing.status == UserStatus.REVOKED:
            outcome = "revoked"
        elif (lock := user_service.lock_remaining_seconds(session, tg_user.id)):
            outcome = "locked"
        elif user_service.verify_access_password(session, password):
            registered = user_service.register_designer(session, tg_user.id, tg_user.full_name, tg_user.username)
            user_service.clear_login_attempts(session, tg_user.id)
            outcome = "ok"
        else:
            failures, lock_seconds = user_service.record_failed_login(
                session, tg_user.id, settings.login_max_failures, settings.login_lockout_minutes
            )
            outcome = "wrong"
    if outcome == "revoked":
        clear_prompt(context)
        await context.bot.send_message(tg_user.id, "🚫 Your access has been revoked. The password cannot re-register this account.")
        return
    if outcome == "locked":
        await context.bot.send_message(tg_user.id, f"⏳ Too many failed attempts. Try again in {_minutes(lock)}.")
        return
    if outcome == "ok":
        clear_prompt(context)
        await context.bot.send_message(
            tg_user.id,
            f"✅ <b>User registered</b>\nRole = {ROLE_LABELS[registered.role]}\n\nYou won't need the password again.\n\n"
            + help_text(registered, private=True),
        )
        log.info("Registered user %s (%s)", tg_user.id, tg_user.full_name)
        return
    if breaker.record():  # too many wrong passwords across ALL accounts: pause registration, alert once
        log.warning("Password breaker opened after %d failures in %d min", settings.password_breaker_failures, settings.password_breaker_window_minutes)
        await notify_user(
            context,
            settings.super_admin_telegram_id,
            f"🚨 Registration paused for {settings.password_breaker_pause_minutes} min: {settings.password_breaker_failures} wrong "
            f"password attempts in {settings.password_breaker_window_minutes} min across all accounts. Consider /setpassword.",
        )
    if lock_seconds:
        clear_prompt(context)
        await context.bot.send_message(tg_user.id, f"🚫 Too many failed attempts. Locked for {_minutes(lock_seconds)}.")
        if tg_user.id != settings.super_admin_telegram_id:
            handle = f" (@{esc(tg_user.username)})" if tg_user.username else ""
            await notify_user(
                context,
                settings.super_admin_telegram_id,
                f"🚨 Login lockout: {esc(tg_user.full_name)}{handle}, Telegram ID <code>{tg_user.id}</code>, "
                f"after {settings.login_max_failures} wrong password attempts.",
            )
    else:
        left = settings.login_max_failures - failures
        await context.bot.send_message(tg_user.id, f"❌ Incorrect password. {left} attempt{'s' if left != 1 else ''} left.")
