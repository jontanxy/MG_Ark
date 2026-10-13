"""Operations shared by command handlers and scheduled jobs (validation runs, group posts, preview sending)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import ContextTypes

from ..constants import SCANNABLE_STATUSES, PreviewStatus, ProjectStatus, build_folder_tree
from ..db import session_scope
from ..models import PreviewAsset, Project, User
from ..services import groups as group_service
from ..services import notifications
from ..services import projects as project_service
from ..services import tracking
from ..services.drive import DriveError
from ..services.listing import build_listing, render_listing
from ..services.previews import PreviewJob, PreviewOrphan, plan_previews, remove_orphans
from ..services.projects import ProjectError
from ..services.sheets import spreadsheet_url
from ..services.validation import ScanResult, latest_report, validate_project
from ..util import esc, human_size
from .access import drive_of, limiter, project_lock, settings_of

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
# Announcement: posted when the archive is created (or with "Announce"), then kept current in place
# ----------------------------------------------------------------------------------------

OUTDATED_ANNOUNCEMENT = "📦 <i>This announcement was replaced — see the latest one below.</i>"


def announcement_text(context: ContextTypes.DEFAULT_TYPE, project: Project) -> str:
    return notifications.announcement(project, tree_of(context))


ANNOUNCEMENT_DELAY_SECONDS = 4.0  # changes made within this time become one edit of the group message


def _announcement_lock(context: ContextTypes.DEFAULT_TYPE, project_id: int) -> asyncio.Lock:
    """Posting and editing a project's announcement happen one at a time, so they never act on an old id."""
    locks: dict[int, asyncio.Lock] = context.bot_data.setdefault("announcement_locks", {})
    return locks.setdefault(project_id, asyncio.Lock())


def _forget_announcement(project_id: int, message_id: int) -> None:
    with session_scope() as session:
        project = session.get(Project, project_id)
        if project is not None and project.announcement_message_id == message_id:  # not a newer one
            project.announcement_message_id = None
            project.announcement_message_hash = None


async def post_announcement(context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project) -> bool:
    """Post the project's announcement to its MG Group and remember it, so later changes edit it in place.

    An earlier announcement of the same project is marked as replaced, so only one copy shows current
    information. Returns False when the bot cannot post there.
    """
    session.commit()  # hand the connection back before waiting: presses queue on the lock and on Telegram's group limit
    async with _announcement_lock(context, project.id):
        session.refresh(project, ["announcement_message_id", "announcement_message_hash", "mg_group_chat_id"])
        chat_before, previous = project.mg_group_chat_id, project.announcement_message_id
        text = announcement_text(context, project)
        session.commit()  # objects stay usable (expire_on_commit=False); no connection is held during the send
        message, _chat_used = await send_to_group(context, chat_before, text)
        if message is None:
            return False
        project.announcement_message_id = message.message_id
        project.announcement_message_hash = _digest(text)
        session.commit()
        if previous is not None and previous != message.message_id:
            try:
                await context.bot.edit_message_text(OUTDATED_ANNOUNCEMENT, chat_id=chat_before, message_id=previous)
            except TelegramError as exc:  # deleted meanwhile, or the group was migrated: nothing to mark
                log.info("Earlier announcement %s/%s not marked as replaced: %s", chat_before, previous, exc)
    return True


async def refresh_announcement(context: ContextTypes.DEFAULT_TYPE, project_id: int) -> bool:
    """Edit the project's announcement in place when what it shows has changed. Never posts a new message.

    No database session is held while waiting for Telegram (group messages are rate limited and may queue).
    Telegram does not notify anyone about an edit, so this is silent. If the message is gone (deleted by a
    group admin, group migrated) it is forgotten; "Announce" posts a new one. Returns True when edited.
    """
    async with _announcement_lock(context, project_id):
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is None:
                return False
            chat_id, message_id = project.mg_group_chat_id, project.announcement_message_id
            if chat_id is None or message_id is None or project.status in (ProjectStatus.DRAFT, ProjectStatus.CANCELLED):
                return False
            if not group_service.is_group_authorised(session, chat_id):
                return False
            text = announcement_text(context, project)
            digest = _digest(text)
            if project.announcement_message_hash == digest:
                return False
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
        except ChatMigrated as exc:  # a basic group became a supergroup: its old messages stay behind
            log.info("Group %s migrated to %s; the announcement of project %s is forgotten", chat_id, exc.new_chat_id, project_id)
            with session_scope() as session:
                group_service.migrate_group(session, chat_id, exc.new_chat_id)
            _forget_announcement(project_id, message_id)
            return False
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.info("Announcement %s/%s could not be edited (%s); forgetting it", chat_id, message_id, exc)
                _forget_announcement(project_id, message_id)
                return False
        except TelegramError as exc:  # pragma: no cover - network; tried again at the next change or check
            log.warning("Announcement edit failed for project %s: %s", project_id, exc)
            return False
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is not None and project.announcement_message_id == message_id:
                project.announcement_message_hash = digest
    return True


def schedule_announcement_refresh(context: ContextTypes.DEFAULT_TYPE, project_id: int) -> None:
    """Bring the announcement up to date a few seconds from now, in the background.

    Several changes in a row (ticking people one after another) become one edit, and no handler waits for
    Telegram's per-group limit. A change made while the edit is under way schedules one more refresh.
    """
    pending: dict[int, asyncio.Task] = context.bot_data.setdefault("announcement_pending", {})
    running: set[asyncio.Task] = context.bot_data.setdefault("announcement_tasks", set())
    waiting = pending.get(project_id)
    if waiting is not None and not waiting.done():
        return  # it has not started yet, so it will read the latest state

    async def _later() -> None:
        try:
            await asyncio.sleep(context.bot_data.get("announcement_delay", ANNOUNCEMENT_DELAY_SECONDS))
        finally:
            if pending.get(project_id) is asyncio.current_task():
                pending.pop(project_id, None)
        try:
            await refresh_announcement(context, project_id)
        except Exception:  # noqa: BLE001 - the announcement must never break anything else
            log.exception("Announcement refresh of project %s failed", project_id)

    task = asyncio.create_task(_later(), name=f"announcement-{project_id}")
    pending[project_id] = task
    running.add(task)
    task.add_done_callback(running.discard)


async def flush_announcement_refreshes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wait for every scheduled announcement refresh (tests, shutdown)."""
    while True:
        tasks = [t for t in context.bot_data.get("announcement_tasks", set()) if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# ----------------------------------------------------------------------------------------
# Live status message: one progress message per project in its MG Group, edited in place
# ----------------------------------------------------------------------------------------

OUTDATED_STATUS = "📊 <i>Outdated — see the latest status message below.</i>"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


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
    """DM a registered, ACTIVE user. Revoked or unknown users never receive project information."""
    if user_id is None:
        return
    with session_scope() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            log.info("Not notifying user %s: not an active user", user_id)
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


async def _repair_folders(session: Session, project: Project, drive, settings) -> None:
    """Bring an open project's folders in line with the layout: forget folders the layout no longer has, create
    declared ones that are missing (Drive failed when the asset was switched on). Never raises."""
    if not project.drive_root_id or project.status not in SCANNABLE_STATUSES:
        return
    forgotten = project_service.forget_obsolete_folders(session, project)
    if forgotten:
        session.commit()
        log.info(
            "Project %s: folders of an earlier layout are no longer tracked (left as they are on Google Drive): %s",
            project.id,
            "; ".join(f"{name} {link}" for _, name, link in forgotten),
        )
    try:
        created = await project_service.ensure_folders(session, project, drive, settings)
    except (ProjectError, DriveError) as exc:
        log.warning("Could not create the missing folders of project %s: %s", project.id, exc)
        return
    if created:
        session.commit()
        log.info("Created missing folders for project %s: %s", project.id, ", ".join(created))


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
        await _repair_folders(session, project, drive, settings)
        result = await validate_project(session, project, drive, settings)
        jobs: list[PreviewJob] = []
        if queue_previews and settings.previews_enabled and not result.report.had_errors:
            jobs = plan_previews(
                session,
                project,
                result.source_files,
                force=force_previews,
                orphans=orphans,
                min_size=settings.preview_min_source_bytes,
                max_size=settings.preview_max_source_bytes,
                max_jobs=settings.preview_max_per_scan,
            )
        session.commit()
    outcome = CheckOutcome(result)
    if result.old_status != result.new_status:
        schedule_sheet_sync(context, project.id)
    if orphans:
        await asyncio.to_thread(remove_orphans, orphans, drive)
    if live != "skip" and not result.report.had_errors:
        await refresh_live_status(context, session, project, live_status_text(context, project, result.report), repost=live == "repost")
    # changes made elsewhere (a designer's access revoked, a role changed) reach the announcement here
    schedule_announcement_refresh(context, project.id)
    if result.became_ready:
        outcome.group_notified = await post_to_group(context, project.mg_group_chat_id, notifications.ready_message(project))
        target = project_owner_id(context, project)
        if requested_by != target:
            await notify_user(context, target, notifications.ready_message(project) + "\n\nOpen /projects to verify it.")
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


def project_owner_id(context: ContextTypes.DEFAULT_TYPE, project: Project) -> int:
    """Who receives a project's DMs: its lead, or the Super Admin while the project has no lead."""
    return project.lead_id if project.lead_id is not None else settings_of(context).super_admin_telegram_id


def _listing_executor(context: ContextTypes.DEFAULT_TYPE) -> ThreadPoolExecutor:
    """Listings run on their own small pool so they can never starve scans, provisioning and previews."""
    pool = context.bot_data.get("listing_executor")
    if pool is None:
        pool = context.bot_data["listing_executor"] = ThreadPoolExecutor(max_workers=2, thread_name_prefix="listing")
    return pool


def may_list_files(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    settings = settings_of(context)
    return limiter(context, "files_user", settings.files_per_user_per_minute, 60).allow(user_id)


async def project_file_listing(context: ContextTypes.DEFAULT_TYPE, project: Project) -> list[str]:
    """Telegram-ready listing of the project's Drive folder.

    Single-flight per project (concurrent callers share one walk), re-read from Drive at most once per
    cooldown window, bounded in folders/files/messages, and run on a dedicated small thread pool.
    """
    settings = settings_of(context)
    cache: dict[int, tuple[float, list[str], str]] = context.bot_data.setdefault("file_listings", {})
    title = project.full_name

    def cached() -> list[str] | None:
        hit = cache.get(project.id)  # an entry made under another name (before a rename or a move) is not used
        return hit[1] if hit and hit[2] == title and time.monotonic() - hit[0] < settings.status_cooldown_seconds else None

    if (chunks := cached()) is not None:
        return chunks
    if not project.drive_root_id:
        return [f"📂 <b>{esc(project.full_name)}</b> has no Google Drive folder."]
    async with project_lock(context, project.id):
        if (chunks := cached()) is not None:  # filled while we waited
            return chunks
        key_order = {spec.key: i for i, spec in enumerate(tree_of(context))}
        known = {f.drive_id: f.key for f in project.folders}
        loop = asyncio.get_running_loop()
        root = await loop.run_in_executor(
            _listing_executor(context), build_listing, drive_of(context), project.drive_root_id, project.name, known, key_order
        )
        if root.error:
            return [f"⚠️ Could not read Google Drive for <b>{esc(project.full_name)}</b>: {esc(root.error[:200])}"]
        chunks = render_listing(title, root, max_chunks=settings.files_max_chunks)
        cache[project.id] = (time.monotonic(), chunks, title)
        return chunks


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
            sheets = context.bot_data["sheets"]
            if row is None:  # revoked: the row disappears and everything below moves up
                outcome = await asyncio.to_thread(tracking.delete_row, sheets, sheet_id, project_id)
            else:
                outcome = await asyncio.to_thread(tracking.upsert_row, sheets, sheet_id, row)
            if outcome == "needs_rebuild":  # column layout changed with a new version: rewrite header and every row
                with session_scope() as session:
                    rows = tracking.all_rows(session, context.bot_data["tz"])  # revoked projects are left out
                await asyncio.to_thread(tracking.rebuild, sheets, sheet_id, rows)
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
