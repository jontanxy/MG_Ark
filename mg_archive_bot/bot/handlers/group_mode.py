"""Commands available inside authorised MG Groups: progress and reminders only."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ...constants import ProjectStatus, Role
from ...db import session_scope
from ...models import User
from ...services import notifications
from ...services import projects as project_service
from ...util import esc
from ..access import limiter, require, settings_of
from ..actions import check_project, may_list_files, project_file_listing, tree_of

log = logging.getLogger(__name__)

OPEN = (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION)
MAX_PER_STATUS = 6


@require(scope="group")
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    chat_id = update.effective_chat.id
    tz = context.bot_data["tz"]
    with session_scope() as session:
        projects = project_service.list_projects(session, OPEN, group_chat_id=chat_id)
        if not projects:
            await update.message.reply_text("No open archives are linked to this group.")
            return
        if len(projects) > MAX_PER_STATUS:
            await update.message.reply_text(f"{len(projects)} open archives — showing the {MAX_PER_STATUS} most recent.")
        # One Drive scan AND one re-post per group per cooldown window: inside the window the live message
        # is already current, so the bot only points at it (and stays silent if the same person keeps asking).
        cooldown = settings_of(context).status_cooldown_seconds
        if not limiter(context, "group_status", 1, cooldown).allow(chat_id):
            if limiter(context, "status_user", 3, 600).allow(actor.telegram_id):
                await update.message.reply_text("ℹ️ Status was refreshed less than a minute ago — see the status message above.")
            return
        for project in projects[:MAX_PER_STATUS]:
            # requested_by=None: a group member asking for status is not the one to receive preview DMs
            report = (await check_project(context, session, project, requested_by=None, live="repost")).result.report
            if report.had_errors:  # never stored as the live message: the last good state stays
                await update.message.reply_text(notifications.progress_message(project, report, tree_of(context), tz))


@require(scope="group")
async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    """Folder-by-folder file listing for this group's open archives."""
    if actor.role == Role.LIGHTS:
        await update.message.reply_text("Lights access is preview-only; ask a Team Lead for the file list.")
        return
    chat_id = update.effective_chat.id
    if not may_list_files(context, actor.telegram_id) or not limiter(context, "files_chat", 1, 60).allow(chat_id):
        return  # per-user and per-chat budget exhausted: stay silent rather than flood the group
    with session_scope() as session:
        projects = project_service.list_projects(session, OPEN, group_chat_id=chat_id)
        if not projects:
            await update.message.reply_text("No open archives are linked to this group.")
            return
        if len(projects) > MAX_PER_STATUS:
            await update.message.reply_text(f"{len(projects)} open archives — showing the {MAX_PER_STATUS} most recent.")
        for project in projects[:MAX_PER_STATUS]:
            for chunk in await project_file_listing(context, project):
                await update.message.reply_text(chunk)


@require(Role.TEAM_LEAD, scope="group")
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    chat_id = update.effective_chat.id
    posted = errors = 0
    with session_scope() as session:
        projects = project_service.list_projects(session, (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE), group_chat_id=chat_id)
        for project in projects[:MAX_PER_STATUS]:
            outcome = await check_project(context, session, project, requested_by=actor.telegram_id)
            if outcome.result.report.had_errors:
                errors += 1
                await update.message.reply_text(f"⚠️ Could not check Google Drive for <b>{esc(project.full_name)}</b> — no reminder sent.")
                continue
            if not outcome.result.report.missing:
                continue
            project_service.mark_reminded(session, project)
            session.commit()
            await update.message.reply_text(notifications.reminder_message(project, outcome.result.report, tree_of(context)))
            posted += 1
    if not posted and not errors:
        await update.message.reply_text("✅ Nothing is missing for this group's open archives.")
