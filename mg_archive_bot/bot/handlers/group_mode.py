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
from ...services.validation import latest_report
from ..access import limiter, require, settings_of
from ..actions import check_project, tree_of

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
        # One Drive scan per group per cooldown window; repeated /status re-uses the stored result.
        fresh = limiter(context, "group_status", 1, settings_of(context).status_cooldown_seconds).allow(chat_id)
        for project in projects[:MAX_PER_STATUS]:
            report = None if fresh else latest_report(session, project)
            if report is None:
                report = (await check_project(context, session, project, requested_by=actor.telegram_id)).result.report
            await update.message.reply_text(notifications.progress_message(project, report, tree_of(context), tz))


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
