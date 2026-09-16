from __future__ import annotations

import logging

from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
from telegram.ext import ContextTypes

from ...constants import ROLE_LABELS, ROLE_RANK, Role
from ...db import session_scope
from ...models import User
from ...services import projects as project_service
from ...util import esc
from ..access import clear_prompt, is_group, is_private, require, resolve_actor

log = logging.getLogger(__name__)


def help_text(user: User | None, *, private: bool) -> str:
    if not private:
        return (
            "<b>MG Group commands</b>\n"
            "/status — archive progress for this group's projects\n"
            "/remind — post reminders for missing uploads (Team Lead)\n"
            "/activate &lt;token&gt; — authorise this group (Team Lead)\n\n"
            "Search, previews and project management happen in a private chat with me."
        )
    rank = ROLE_RANK[user.role] if user else -1
    lines = [
        "<b>Commands</b>",
        "/search &lt;terms&gt; — find archived projects (comma-separated, e.g. <code>worship, gold</code>)",
        "…or just type keywords to search.",
    ]
    if rank >= ROLE_RANK[Role.TEAM_LEAD]:
        lines += [
            "",
            "<b>Team Lead</b>",
            "/newproject — create a new archive (folders + announcement)",
            "/projects — manage archives: progress, reminders, designers, metadata, verification",
            "/creategroup — Create MG Group: token to authorise a new group",
            "/sheet — link to the project index sheet (one row per project)",
        ]
    if rank >= ROLE_RANK[Role.SUPER_ADMIN]:
        lines += [
            "",
            "<b>Super Admin</b>",
            "/users — view users, change roles, revoke/restore",
            "/groups — view and revoke MG Groups",
            "/setpassword — change the access password",
        ]
    lines += ["", "/whoami — your role · /cancel — abort the current step · /help — this list"]
    return "\n".join(lines)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_group(update):
        await update.message.reply_text(help_text(None, private=False))
        return
    if not is_private(update):
        return
    actor = resolve_actor(update, context)
    if actor is None or not actor.is_active:
        await update.message.reply_text("Send /start to register with the access password.")
        return
    await update.message.reply_text(help_text(actor, private=True))


@require(scope="private")
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    await update.message.reply_text(
        f"<b>{esc(actor.display_name)}</b>\nRole: {ROLE_LABELS[actor.role]}\nStatus: {actor.status.value}\nTelegram ID: <code>{actor.telegram_id}</code>"
    )


@require(scope="private")
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    clear_prompt(context)
    wizard = context.user_data.pop("wizard", None)
    if wizard:
        with session_scope() as session:
            project = project_service.get_project(session, wizard.get("project_id", 0))
            if project is not None and project.status.value == "DRAFT" and project.created_by == actor.telegram_id:
                project_service.delete_draft(session, project)
    await update.message.reply_text("Cancelled.")


async def unknown_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update) and update.message is not None:
        await update.message.reply_text("Unknown command. Send /help to see what I can do.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Network error: %s", err)
        return
    if isinstance(err, Forbidden):
        log.info("Forbidden: %s", err)
        return
    log.exception("Unhandled error while processing update %s", getattr(update, "update_id", "?"), exc_info=err)
    if isinstance(update, Update) and update.effective_chat is not None and is_private(update):
        try:
            text = "⚠️ Something went wrong. Please try again."
            if isinstance(err, BadRequest):
                text += f"\n<code>{str(err)[:200]}</code>"
            await context.bot.send_message(update.effective_chat.id, text)
        except Exception:  # pragma: no cover - best effort
            log.debug("could not report error to user")
