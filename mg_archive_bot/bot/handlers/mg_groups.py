from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ...constants import ROLE_RANK, Role
from ...db import session_scope
from ...models import User
from ...services import groups as group_service
from ...services import users as user_service
from ...util import esc
from ..access import require, settings_of

log = logging.getLogger(__name__)

JOINED = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.RESTRICTED}
GONE = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}


def _activation_hint() -> str:
    return (
        "🔒 This group is <b>not yet an authorised MG Group</b>.\n\n"
        "A Team Lead: get a token with /creategroup in a private chat with me, then send here:\n"
        "<code>/activate MG-XXXX-XXXX</code>"
    )


@require(Role.TEAM_LEAD)
async def cmd_creategroup(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    settings = settings_of(context)
    with session_scope() as session:
        token = group_service.create_token(session, actor.telegram_id, settings.provisioning_token_ttl_hours)
        value = token.token
    me = await context.bot.get_me()
    await update.message.reply_text(
        "💬 <b>Create MG Group</b>\n\n"
        f"Your provisioning token: <code>{value}</code> (valid {settings.provisioning_token_ttl_hours} h, single use)\n\n"
        "1. Create the Telegram group.\n"
        f"2. Add @{esc(me.username)} to it.\n"
        "3. If you added the bot yourself, the group is authorised automatically.\n"
        f"   Otherwise send <code>/activate {value}</code> inside the group.\n\n"
        "Only groups authorised this way can use archive announcements and progress tracking."
    )


@require(Role.TEAM_LEAD, scope="group", group_auth=False)
async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    chat = update.effective_chat
    raw = " ".join(context.args or [])
    with session_scope() as session:
        if group_service.is_group_authorised(session, chat.id):
            await update.message.reply_text("✅ This group is already an authorised MG Group.")
            return
        token = group_service.find_valid_token(session, raw) if raw else None
        if token is None:
            await update.message.reply_text("❌ Invalid or expired token. Get a new one with /creategroup in a private chat.")
            return
        try:
            group_service.authorise_group(session, chat.id, chat.title or "", token)
        except group_service.GroupError as exc:
            await update.message.reply_text(f"❌ {esc(str(exc))}")
            return
    await update.message.reply_text(f"✅ <b>{esc(chat.title or 'This group')}</b> is now an authorised MG Group.\n\nI'll post archive announcements and progress here. Use /status any time.")
    log.info("Group %s authorised via /activate by %s", chat.id, actor.telegram_id)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The bot was added to / removed from a chat."""
    change = update.my_chat_member
    if change is None:
        return
    chat = change.chat
    old, new = change.old_chat_member.status, change.new_chat_member.status
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if chat.type == ChatType.CHANNEL and new in JOINED:
            try:
                await context.bot.leave_chat(chat.id)
            except TelegramError:
                pass
        return
    settings = settings_of(context)
    if new in JOINED and old not in JOINED:
        adder = change.from_user
        with session_scope() as session:
            if group_service.is_group_authorised(session, chat.id):
                group_service.update_group_title(session, chat.id, chat.title or "")
                session.commit()
                await context.bot.send_message(chat.id, "✅ Reconnected to this authorised MG Group.")
                return
            adder_user = user_service.get_user(session, adder.id) if adder else None
            if adder and adder.id == settings.super_admin_telegram_id:
                adder_user = user_service.ensure_super_admin(session, adder.id, adder.full_name, adder.username)
            tokens = group_service.pending_tokens_for(session, adder.id) if adder else []
            existing = group_service.get_group(session, chat.id)
            if existing is not None and existing.revoked_by_admin:
                group_service.note_unauthorised_chat(session, chat.id, chat.title or "")  # so the leave job evicts us again
                session.commit()
                await context.bot.send_message(chat.id, "🔒 This group was revoked by the Super Admin. Ask them to restore it from /groups.")
                return
            if (
                adder_user is not None
                and adder_user.is_active
                and ROLE_RANK[adder_user.role] >= ROLE_RANK[Role.TEAM_LEAD]
                and len(tokens) == 1
            ):
                group_service.authorise_group(session, chat.id, chat.title or "", tokens[0])
                session.commit()
                await context.bot.send_message(
                    chat.id,
                    f"✅ <b>{esc(chat.title or 'This group')}</b> is now an authorised MG Group (token from {esc(adder_user.display_name)}).\n\nI'll post archive announcements and progress here. Use /status any time.",
                )
                log.info("Group %s auto-authorised by %s", chat.id, adder.id)
                return
            group_service.note_unauthorised_chat(session, chat.id, chat.title or "")
        await context.bot.send_message(chat.id, _activation_hint())
        return
    if new in GONE and old in JOINED:
        with session_scope() as session:
            group = group_service.get_group(session, chat.id)
            if group is not None and group.is_active:
                group_service.revoke_group(session, chat.id, revoked_by=None)
                log.info("Bot removed from authorised group %s; marked revoked", chat.id)
            group_service.forget_unauthorised_chat(session, chat.id)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A group was upgraded to a supergroup: its chat_id changes, keep our records in sync."""
    msg = update.message
    if msg is None or msg.migrate_to_chat_id is None:
        return
    old_id, new_id = msg.chat.id, msg.migrate_to_chat_id
    with session_scope() as session:
        moved = group_service.migrate_group(session, old_id, new_id)
    if moved:
        log.info("Group %s migrated to %s", old_id, new_id)
