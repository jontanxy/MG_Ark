from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ...constants import ROLE_LABELS, Role, UserStatus
from ...db import session_scope
from ...models import User
from ...services import groups as group_service
from ...services import users as user_service
from ...util import esc, fmt_dt
from ..access import clear_prompt, require, safe_edit, set_prompt, settings_of
from ..keyboards import (
    confirm_revoke_group_keyboard,
    groups_list_keyboard,
    user_card_keyboard,
    users_list_keyboard,
)

log = logging.getLogger(__name__)


def _users_text(users: list[User]) -> str:
    active = sum(1 for u in users if u.status == UserStatus.ACTIVE)
    return f"👥 <b>Authorised users</b> — {active} active, {len(users) - active} revoked\n\nTap a user to manage them."


def _user_card(user: User, context: ContextTypes.DEFAULT_TYPE) -> str:
    tz = context.bot_data["tz"]
    handle = f" (@{esc(user.username)})" if user.username else ""
    return (
        f"👤 <b>{esc(user.display_name)}</b>{handle}\n"
        f"Role: {ROLE_LABELS[user.role]}\nStatus: {user.status.value}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\nRegistered: {esc(fmt_dt(user.created_at, tz))}"
    )


def _groups_text(groups) -> str:
    if not groups:
        return "💬 <b>MG Groups</b>\n\nNo groups authorised yet. A Team Lead can run /creategroup."
    lines = ["💬 <b>MG Groups</b>", ""]
    for g in groups:
        icon = "🟢" if g.status.value == "ACTIVE" else "🚫"
        lines.append(f"{icon} <b>{esc(g.title or 'untitled')}</b> — <code>{g.chat_id}</code> ({g.status.value.lower()})")
    return "\n".join(lines)


@require(Role.SUPER_ADMIN)
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    with session_scope() as session:
        users = user_service.list_users(session)
        await update.message.reply_text(_users_text(users), reply_markup=users_list_keyboard(users))


@require(Role.SUPER_ADMIN)
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    with session_scope() as session:
        groups = group_service.list_groups(session)
        await update.message.reply_text(_groups_text(groups), reply_markup=groups_list_keyboard(groups))


@require(Role.SUPER_ADMIN)
async def cmd_setpassword(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    set_prompt(context, "new_password")
    await update.message.reply_text(
        "🔐 Send the <b>new access password</b> (at least 8 characters).\nExisting users keep their access; only new registrations use it.\n/cancel to abort."
    )


async def handle_new_password(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    text = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except TelegramError:
        pass
    if actor.role != Role.SUPER_ADMIN:
        clear_prompt(context)
        await context.bot.send_message(actor.telegram_id, "Only the Super Admin can change the password.")
        return
    with session_scope() as session:
        try:
            user_service.set_access_password(session, text)
        except user_service.UserError as exc:
            await context.bot.send_message(actor.telegram_id, f"❌ {esc(str(exc))} Send another password or /cancel.")
            return
    clear_prompt(context)
    await context.bot.send_message(actor.telegram_id, "✅ Access password updated.")
    log.info("Access password changed by %s", actor.telegram_id)


@require(Role.SUPER_ADMIN)
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    # A callback query may be answered exactly once (Telegram rejects a second answerCallbackQuery),
    # and every DB change is committed before any Telegram call.
    query = update.callback_query
    parts = query.data.split(":")
    settings = settings_of(context)
    with session_scope() as session:
        if parts[1] == "users":
            users = user_service.list_users(session)
            await query.answer()
            await safe_edit(update, _users_text(users), users_list_keyboard(users))
            return
        if parts[1] == "groups":
            groups = group_service.list_groups(session)
            await query.answer()
            await safe_edit(update, _groups_text(groups), groups_list_keyboard(groups))
            return
        if parts[1] == "u":
            user_id = int(parts[2])
            action = parts[3] if len(parts) > 3 else "show"
            toast = None
            try:
                if action == "role":
                    user = user_service.set_role(session, user_id, Role(parts[4]), settings.super_admin_telegram_id)
                    toast = f"Role set to {ROLE_LABELS[user.role]}"
                elif action == "revoke":
                    user = user_service.revoke_user(session, user_id, settings.super_admin_telegram_id)
                    toast = "Access revoked"
                elif action == "restore":
                    user = user_service.restore_user(session, user_id)
                    toast = "Access restored"
                else:
                    user = user_service.get_user(session, user_id)
            except user_service.UserError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            session.commit()
            await query.answer(toast)
            if user is None:
                await safe_edit(update, "User not found.")
                return
            await safe_edit(update, _user_card(user, context), user_card_keyboard(user, settings.super_admin_telegram_id))
            return
        if parts[1] == "g":
            chat_id = int(parts[2])
            action = parts[3] if len(parts) > 3 else ""
            group = group_service.get_group(session, chat_id)
            if group is None:
                await query.answer("Group not found", show_alert=True)
                return
            if action == "revoke":
                await query.answer()
                await safe_edit(
                    update,
                    f"Revoke <b>{esc(group.title or chat_id)}</b>?\nThe bot will leave the group and stop posting there.",
                    confirm_revoke_group_keyboard(chat_id),
                )
                return
            if action == "restore":
                group_service.restore_group(session, chat_id)
                session.commit()
                await query.answer("Group restored — re-add the bot if it left")
                groups = group_service.list_groups(session)
                await safe_edit(update, "♻️ Group restored. If the bot left the group, add it again (no token needed).\n\n" + _groups_text(groups), groups_list_keyboard(groups))
                return
            if action == "revoke2":
                group_service.revoke_group(session, chat_id, revoked_by=actor.telegram_id)
                session.commit()
                await query.answer("Group revoked")
                try:
                    await context.bot.leave_chat(chat_id)
                except TelegramError as exc:
                    log.info("leave_chat(%s) failed: %s", chat_id, exc)
                groups = group_service.list_groups(session)
                await safe_edit(update, "🚫 Group revoked.\n\n" + _groups_text(groups), groups_list_keyboard(groups))
                return
            await query.answer()
            return
        await query.answer()
