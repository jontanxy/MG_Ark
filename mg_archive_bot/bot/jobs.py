"""Scheduled jobs and the background preview worker."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..constants import SCANNABLE_STATUSES, PreviewStatus, ProjectStatus
from ..db import session_scope
from ..models import PreviewAsset
from ..services import groups as group_service
from ..services import notifications
from ..services import projects as project_service
from ..services.drive import file_link
from ..services.previews import PreviewJob, kill_active_transcodes, process_preview, record_result
from ..util import esc, human_size, utcnow
from .access import drive_of, settings_of
from .actions import check_project, notify_user, post_to_group, rebuild_sheet, tree_of

log = logging.getLogger(__name__)


class PreviewWorker:
    """Single consumer that turns ProRes sources into MP4 previews one at a time."""

    def __init__(self, bot, bot_data: dict) -> None:
        self.bot = bot
        self.bot_data = bot_data
        self.queue: asyncio.Queue[PreviewJob] = asyncio.Queue()
        self.pending: set[tuple[int, str, str]] = set()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="preview-worker")

    async def stop(self) -> None:
        killed = kill_active_transcodes()
        if killed:
            log.info("Stopped %d running ffmpeg process(es)", killed)
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def enqueue(self, jobs: list[PreviewJob]) -> int:
        added = 0
        for job in jobs:
            if job.dedupe_key in self.pending:
                continue
            self.pending.add(job.dedupe_key)
            self.queue.put_nowait(job)
            added += 1
        return added

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("Preview job crashed: %s", job.source.name)
            finally:
                self.pending.discard(job.dedupe_key)
                self.queue.task_done()

    async def _process(self, job: PreviewJob) -> None:
        settings = settings_of(self._ctx())
        drive = drive_of(self._ctx())
        with session_scope() as session:
            row = session.get(PreviewAsset, job.preview_id)
            if row is None or row.source_fingerprint != job.source.fingerprint:
                return
            existing_drive_id = row.preview_drive_id
            project_name = row.project.full_name
            created_by = row.project.created_by
        result = await asyncio.to_thread(process_preview, job, drive, settings, existing_drive_id)
        with session_scope() as session:
            record_result(session, job.preview_id, result)
        target = job.requested_by
        if target is None and result.status == PreviewStatus.FAILED:
            target = created_by  # unattended scan: the Team Lead who owns the project should know
        if target is None:
            return
        if result.status == PreviewStatus.READY:
            link = f'<a href="{file_link(result.preview_drive_id)}">open on Drive</a>' if result.preview_drive_id else ""
            text = f"✅ Preview ready: <b>{esc(project_name)}</b> — {esc(result.preview_name)} ({human_size(result.size_bytes)}) {link}"
        else:
            text = f"❌ Preview failed: <b>{esc(project_name)}</b> — {esc(job.source.name)}\n<code>{esc(result.error[:300])}</code>"
        await notify_user(self._ctx(), target, text)

    def _ctx(self) -> SimpleNamespace:
        # The helpers only need ``bot`` and ``bot_data``; a lightweight context object is enough.
        return SimpleNamespace(bot=self.bot, bot_data=self.bot_data)


async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        ids = [p.id for p in project_service.list_projects(session, SCANNABLE_STATUSES)]
    log.info("Scan job: %d project(s)", len(ids))
    for pid in ids:
        try:
            with session_scope() as session:
                project = project_service.get_project(session, pid)
                if project is None or project.status not in SCANNABLE_STATUSES:
                    continue
                await check_project(context, session, project, requested_by=None)
        except Exception:  # noqa: BLE001
            log.exception("Scan failed for project %s", pid)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_of(context)
    gap = timedelta(hours=settings.reminder_min_gap_hours)
    now = utcnow()
    with session_scope() as session:
        ids = [
            p.id
            for p in project_service.list_projects(session, (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE))
            if p.mg_group_chat_id and (p.last_reminder_at is None or now - p.last_reminder_at >= gap)
        ]
    for pid in ids:
        try:
            with session_scope() as session:
                project = project_service.get_project(session, pid)
                if project is None or not project.mg_group_chat_id:
                    continue
                if not group_service.is_group_authorised(session, project.mg_group_chat_id):
                    continue
                outcome = await check_project(context, session, project, requested_by=None)
                if outcome.result.report.had_errors or not outcome.result.report.missing:
                    continue
                if await post_to_group(context, project.mg_group_chat_id, notifications.reminder_message(project, outcome.result.report, tree_of(context))):
                    project_service.mark_reminded(session, project)
        except Exception:  # noqa: BLE001
            log.exception("Reminder failed for project %s", pid)


async def rebuild_sheet_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nightly reconciliation of the project index sheet (fixes any row that a live sync missed)."""
    if not settings_of(context).tracking_sheet_enabled or "sheets" not in context.bot_data:
        return
    try:
        count, _ = await rebuild_sheet(context)
        log.info("Project index rebuilt: %d rows", count)
    except Exception as exc:  # noqa: BLE001
        log.warning("Nightly index rebuild failed: %s", exc)


async def leave_stale_chats_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_of(context)
    if settings.unauthorised_group_leave_minutes <= 0:
        return
    with session_scope() as session:
        stale = [(c.chat_id, c.title) for c in group_service.stale_unauthorised_chats(session, settings.unauthorised_group_leave_minutes)]
    for chat_id, title in stale:
        with session_scope() as session:
            if group_service.is_group_authorised(session, chat_id):
                group_service.forget_unauthorised_chat(session, chat_id)
                continue
        try:
            await context.bot.send_message(chat_id, "Leaving: this group was not authorised as an MG Group. Add me again once a Team Lead has a token from /creategroup.")
            await context.bot.leave_chat(chat_id)
        except TelegramError as exc:
            log.info("Could not leave chat %s (%s): %s", chat_id, title, exc)
        with session_scope() as session:
            group_service.forget_unauthorised_chat(session, chat_id)
