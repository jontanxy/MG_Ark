"""Operations shared by command handlers and scheduled jobs (validation runs, group posts, preview sending)."""
from __future__ import annotations

import asyncio
import hashlib
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
from ..services import tracking
from ..services.previews import PreviewJob, PreviewOrphan, plan_previews, remove_orphans
from ..services.sheets import spreadsheet_url
from ..services.validation import ScanResult, latest_report, validate_project
from ..util import esc, human_size
from .access import drive_of, project_lock, settings_of

log = logging.getLogger(__name__)


def tree_of(context: ContextTypes.DEFAULT_TYPE):
    return build_folder_tree(settings_of(context).folder_names())


async def send_to_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, text: str):
    """Send *text* to an authorised MG group. Returns (message, chat id actually used) or (None, None)."""
    if chat_id is None:
        return None, None
    with session_scope() as session:
        authorised = group_service.is_group_authorised(session, chat_id)
    if not authorised:
        log.info("Not posting to chat %s: not an authorised MG Group", chat_id)
        return None, None
    try:
        return await context.bot.send_message(chat_id, text), chat_id
    except ChatMigrated as exc:
        new_id = exc.new_chat_id
        log.info("Group %s migrated to %s; updating records", chat_id, new_id)
        with session_scope() as session:
            group_service.migrate_group(session, chat_id, new_id)
        try:
            return await context.bot.send_message(new_id, text), new_id
        except TelegramError as exc2:
            log.warning("Cannot post to migrated group %s: %s", new_id, exc2)
            return None, None
    except (Forbidden, BadRequest) as exc:
        log.warning("Cannot post to group %s: %s", chat_id, exc)
        return None, None
    except TelegramError as exc:  # pragma: no cover - network
        log.warning("Telegram error posting to group %s: %s", chat_id, exc)
        return None, None


async def post_to_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, text: str) -> bool:
    """Send *text* to an MG group. Returns False (and logs) when the bot cannot post there."""
    message, _ = await send_to_group(context, chat_id, text)
    return message is not None


# ----------------------------------------------------------------------------------------
# Live status message: one progress message per project in its MG Group, edited in place
# ----------------------------------------------------------------------------------------

OUTDATED_STATUS = "📊 <i>Outdated — see the latest status message below.</i>"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def live_status_text(context: ContextTypes.DEFAULT_TYPE, project: Project, report) -> str:
    return notifications.progress_message(project, report, tree_of(context), context.bot_data["tz"])


async def refresh_live_status(
    context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, text: str, *, repost: bool = False
) -> bool:
    """Keep the project's live status message current.

    * ``repost=False`` (scans, private checks, state changes): silently edit the existing message in place.
      Nothing is ever *created* this way, so background work never adds messages to the chat.
    * ``repost=True`` (``/status``): move the message to the bottom of the chat with fresh content — the old
      copy is deleted, or marked outdated when Telegram no longer allows deleting it (older than 48 h).
    Returns True when a message was edited or sent.
    """
    chat_id = project.mg_group_chat_id
    if chat_id is None:
        return False
    if not group_service.is_group_authorised(session, chat_id):
        return False
    digest = _digest(text)
    message_id = project.status_message_id
    if not repost:
        if message_id is None or project.status_message_hash == digest:
            return False
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                project.status_message_hash = digest
                session.commit()
                return False
            # Deleted by a group admin, or otherwise gone: forget it; the next /status re-creates it.
            log.info("Live status %s/%s could not be edited (%s)", chat_id, message_id, exc)
            project.status_message_id = None
            project.status_message_hash = None
            session.commit()
            return False
        except TelegramError as exc:  # pragma: no cover - network
            log.warning("Live status edit failed for %s: %s", project.full_name, exc)
            return False
        project.status_message_hash = digest
        session.commit()
        return True
    if message_id is not None:
        try:
            await context.bot.delete_message(chat_id, message_id)
        except TelegramError:
            try:
                await context.bot.edit_message_text(OUTDATED_STATUS, chat_id=chat_id, message_id=message_id)
            except TelegramError:
                pass
    message, used_chat = await send_to_group(context, chat_id, text)
    if message is None:
        return False
    if used_chat != chat_id:
        project.mg_group_chat_id = used_chat
    project.status_message_id = message.message_id
    project.status_message_hash = digest
    session.commit()
    return True


async def refresh_live_status_from_latest(context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project) -> bool:
    """After verify / reopen / restore / revoke: reflect the new state in the live message (if there is one)."""
    report = latest_report(session, project)
    if report is None:
        return False
    return await refresh_live_status(context, session, project, live_status_text(context, project, report))


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
    live: str = "edit",
) -> CheckOutcome:
    """Validate a project against Drive, transition its status, notify the group on READY, queue previews.

    ``live`` controls the group's live status message: "edit" refreshes it in place (silent), "repost" moves it
    to the bottom of the chat (used by /status), "skip" leaves it alone.
    """
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
    if result.old_status != result.new_status:
        schedule_sheet_sync(context, project.id)
    if orphans:
        await asyncio.to_thread(remove_orphans, orphans, drive)
    if live != "skip" and not result.report.had_errors:
        await refresh_live_status(context, session, project, live_status_text(context, project, result.report), repost=live == "repost")
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


# ----------------------------------------------------------------------------------------
# Project index sheet
# ----------------------------------------------------------------------------------------


def _sheet_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    lock = context.bot_data.get("sheet_lock")
    if lock is None:
        lock = context.bot_data["sheet_lock"] = asyncio.Lock()
    return lock


async def _report_sheet_failure(context: ContextTypes.DEFAULT_TYPE, exc: Exception) -> None:
    log.warning("Project index sheet sync failed: %s", exc)
    if not context.bot_data.get("sheet_failure_reported"):
        context.bot_data["sheet_failure_reported"] = True
        await notify_user(
            context,
            settings_of(context).super_admin_telegram_id,
            f"⚠️ The project index sheet could not be updated: {esc(str(exc)[:400])}\n\n"
            "The bot keeps working; run /sheet rebuild once the problem is fixed.",
        )


async def sheet_location(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str]:
    """(spreadsheet id, url) of the project index, creating the spreadsheet on first use."""
    settings, drive = settings_of(context), drive_of(context)
    sheets = context.bot_data["sheets"]
    if settings.tracking_sheet_id:
        return settings.tracking_sheet_id, spreadsheet_url(settings.tracking_sheet_id)

    def _stored() -> tuple[str | None, str | None]:
        with session_scope() as session:  # short read; never held across an await
            return tracking.stored_sheet(session)

    sheet_id, url = _stored()
    if sheet_id:
        return sheet_id, url or spreadsheet_url(sheet_id)
    async with _sheet_lock(context):
        sheet_id, url = _stored()  # another task may have created it while we waited
        if sheet_id:
            return sheet_id, url or spreadsheet_url(sheet_id)
        sheet_id, url = await asyncio.to_thread(tracking.create_sheet, drive, sheets, settings)
        with session_scope() as session:
            tracking.remember_sheet(session, sheet_id, url)
        return sheet_id, url


async def sync_project_row(context: ContextTypes.DEFAULT_TYPE, project_id: int) -> None:
    """Write/refresh one project's row in the index sheet. Never raises (failures are logged + reported once)."""
    try:
        sheet_id, _ = await sheet_location(context)
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is None or project.status.value == "DRAFT":
                return
            row = None if project.status.value == "CANCELLED" else tracking.build_row(session, project, context.bot_data["tz"])
        async with _sheet_lock(context):
            if row is None:  # revoked: the row disappears and everything below moves up
                await asyncio.to_thread(tracking.delete_row, context.bot_data["sheets"], sheet_id, project_id)
            else:
                await asyncio.to_thread(tracking.upsert_row, context.bot_data["sheets"], sheet_id, row)
        context.bot_data.pop("sheet_failure_reported", None)
    except Exception as exc:  # noqa: BLE001 - the index must never break the main flow
        await _report_sheet_failure(context, exc)


def schedule_sheet_sync(context: ContextTypes.DEFAULT_TYPE, project_id: int) -> None:
    """Fire-and-forget row sync so handlers stay fast. Use ``flush_sheet_syncs`` to await them (tests, shutdown)."""
    if not settings_of(context).tracking_sheet_enabled or "sheets" not in context.bot_data:
        return
    tasks: set[asyncio.Task] = context.bot_data.setdefault("sheet_tasks", set())
    task = asyncio.create_task(sync_project_row(context, project_id), name=f"sheet-sync-{project_id}")
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def flush_sheet_syncs(context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = list(context.bot_data.get("sheet_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def rebuild_sheet(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, str]:
    """Rewrite the whole index from the database. Returns (rows, url). Raises on failure."""
    sheet_id, url = await sheet_location(context)
    with session_scope() as session:
        rows = tracking.all_rows(session, context.bot_data["tz"])
    async with _sheet_lock(context):
        count = await asyncio.to_thread(tracking.rebuild, context.bot_data["sheets"], sheet_id, rows)
    context.bot_data.pop("sheet_failure_reported", None)
    return count, url
