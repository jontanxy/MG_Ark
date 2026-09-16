"""Operations shared by command handlers and scheduled jobs (validation runs, group posts, preview sending)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import ContextTypes

from ..constants import PreviewStatus, build_folder_tree
from ..db import session_scope
from ..models import PreviewAsset, Project, User
from ..services import groups as group_service
from ..services import notifications
from ..services.previews import PreviewJob, PreviewOrphan, plan_previews, remove_orphans
from ..services.validation import ScanResult, validate_project
from ..util import esc, human_size
from .access import drive_of, project_lock, settings_of

log = logging.getLogger(__name__)


def tree_of(context: ContextTypes.DEFAULT_TYPE):
    return build_folder_tree(settings_of(context).folder_names())


async def post_to_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, text: str) -> bool:
    """Send *text* to an MG group. Returns False (and logs) when the bot cannot post there."""
    if chat_id is None:
        return False
    with session_scope() as session:
        authorised = group_service.is_group_authorised(session, chat_id)
    if not authorised:
        log.info("Not posting to chat %s: not an authorised MG Group", chat_id)
        return False
    try:
        await context.bot.send_message(chat_id, text)
        return True
    except ChatMigrated as exc:
        new_id = exc.new_chat_id
        log.info("Group %s migrated to %s; updating records", chat_id, new_id)
        with session_scope() as session:
            group_service.migrate_group(session, chat_id, new_id)
        try:
            await context.bot.send_message(new_id, text)
            return True
        except TelegramError as exc2:
            log.warning("Cannot post to migrated group %s: %s", new_id, exc2)
            return False
    except (Forbidden, BadRequest) as exc:
        log.warning("Cannot post to group %s: %s", chat_id, exc)
        return False
    except TelegramError as exc:  # pragma: no cover - network
        log.warning("Telegram error posting to group %s: %s", chat_id, exc)
        return False


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int | None, text: str) -> None:
    if user_id is None:
        return
    try:
        await context.bot.send_message(user_id, text)
    except TelegramError as exc:
        log.info("Cannot DM user %s: %s", user_id, exc)


@dataclass
class CheckOutcome:
    result: ScanResult
    queued_previews: int = 0
    group_notified: bool = False


async def check_project(
    context: ContextTypes.DEFAULT_TYPE,
    session: Session,
    project: Project,
    *,
    requested_by: int | None,
    queue_previews: bool = True,
    force_previews: bool = False,
) -> CheckOutcome:
    """Validate a project against Drive, transition its status, notify the group on READY, queue previews."""
    settings = settings_of(context)
    drive = drive_of(context)
    orphans: list[PreviewOrphan] = []
    async with project_lock(context, project.id):
        session.refresh(project)  # another scan may have committed while we waited for the lock
        result = await validate_project(session, project, drive, settings)
        jobs: list[PreviewJob] = []
        if queue_previews and settings.previews_enabled and not result.report.had_errors:
            jobs = plan_previews(session, project, result.source_files, force=force_previews, orphans=orphans)
        session.commit()
    outcome = CheckOutcome(result)
    if orphans:
        await asyncio.to_thread(remove_orphans, orphans, drive)
    if result.became_ready:
        outcome.group_notified = await post_to_group(context, project.mg_group_chat_id, notifications.ready_message(project))
        if requested_by != project.created_by:
            await notify_user(context, project.created_by, notifications.ready_message(project) + "\n\nOpen /projects to verify it.")
    if jobs:
        worker = context.bot_data.get("preview_worker")
        if worker is not None:
            jobs = [PreviewJob(j.project_id, j.preview_id, j.source_key, j.source, j.previews_folder_id, requested_by) for j in jobs]
            outcome.queued_previews = await worker.enqueue(jobs)
    return outcome


async def send_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, preview_id: int) -> None:
    """Hand out the Google Drive link of one MP4 preview (privately). Nothing is uploaded to Telegram."""
    with session_scope() as session:
        preview = session.get(PreviewAsset, preview_id)
        if preview is None:
            await context.bot.send_message(chat_id, "That preview no longer exists.")
            return
        project_name = preview.project.full_name
        status, link, size = preview.status, preview.preview_link, preview.size_bytes
        name = preview.preview_name or preview.source_name
    caption = f"🎬 <b>{esc(project_name)}</b> — {esc(name)}"
    if status != PreviewStatus.READY or not link:
        await context.bot.send_message(chat_id, f"{caption}\n\nPreview is not ready yet ({status.value.lower()}).")
        return
    await context.bot.send_message(
        chat_id,
        f"{caption} ({human_size(size)})",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Play preview on Google Drive", url=link)]]),
    )


def user_by_id(session: Session, user_id: int | None) -> User | None:
    return session.get(User, user_id) if user_id is not None else None
