from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from mg_archive_bot.constants import AssetCategory, PreviewStatus, ProjectStatus
from mg_archive_bot.db import session_scope
from mg_archive_bot.models import PreviewAsset
from mg_archive_bot.services import groups as group_service
from mg_archive_bot.services import projects as project_service
from mg_archive_bot.services import users as user_service
from mg_archive_bot.services.previews import PreviewJob
from mg_archive_bot.services.validation import validate_project
from mg_archive_bot.util import utcnow
from tests.fakes import BotHarness, FakeContext


@pytest.mark.asyncio
async def test_scan_and_reminder_jobs(db, settings, drive):
    from mg_archive_bot.bot.jobs import leave_stale_chats_job, reminder_job, scan_job

    harness = BotHarness(settings, drive)
    ctx = FakeContext(harness.bot, harness.bot_data, {})
    with session_scope() as s:
        user_service.register_designer(s, 11, "Alice", None)
        token = group_service.create_token(s, 1, 24)
        group_service.authorise_group(s, -100, "MG", token)
        p = project_service.create_draft(s, "Scan Me", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.TIMELINE, True)
        await project_service.provision_folders(s, p, drive, settings)
        project_service.set_group(s, p, -100)
        project_service.toggle_assignment(s, p, 11, AssetCategory.ALL)
        pid = p.id
        folders = {f.key: f.drive_id for f in p.folders}
        group_service.note_unauthorised_chat(s, -200, "Stale")
    await scan_job(ctx)
    with session_scope() as s:
        assert project_service.get_project(s, pid).status == ProjectStatus.INCOMPLETE
    await reminder_job(ctx)
    assert harness.bot.last(-100)["text"].startswith("⏰") and "Alice" in harness.bot.last(-100)["text"]
    n = len(harness.bot.sent)
    await reminder_job(ctx)  # within the minimum gap → no second reminder
    assert len(harness.bot.sent) == n
    with session_scope() as s:
        project_service.get_project(s, pid).last_reminder_at = utcnow() - timedelta(hours=30)
    for key in ("fonts", "ae", "timeline_prores", "timeline_hap"):
        drive.put_file(folders[key], f"{key}.bin")
    await reminder_job(ctx)  # complete now → no reminder, but READY announced by the scan inside
    assert "Ready for Team Lead verification" in harness.bot.last(-100)["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).status == ProjectStatus.READY_FOR_VERIFICATION
    await leave_stale_chats_job(ctx)  # chat noted a moment ago → not stale yet
    assert harness.bot.left == []
    with session_scope() as s:
        s.get(__import__("mg_archive_bot.models", fromlist=["UnauthorisedChat"]).UnauthorisedChat, -200).first_seen_at = utcnow() - timedelta(hours=2)
    await leave_stale_chats_job(ctx)
    assert harness.bot.left == [-200]
    with session_scope() as s:
        assert not group_service.stale_unauthorised_chats(s, 0)


@pytest.mark.asyncio
async def test_preview_worker_end_to_end(db, settings, drive, fake_ffmpeg):
    from mg_archive_bot.bot.jobs import PreviewWorker

    settings.ffmpeg_path = str(fake_ffmpeg)
    harness = BotHarness(settings, drive)
    worker = PreviewWorker(harness.bot, harness.bot_data)
    with session_scope() as s:
        p = project_service.create_draft(s, "Preview Me", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.CONTIN_VIDEOS, True)
        await project_service.provision_folders(s, p, drive, settings)
        drive.put_file(p.folder("contin_prores").drive_id, "Cross.mov", content=b"prores", md5="c1", mime_type="video/quicktime")
        result = await validate_project(s, p, drive, settings)
        from mg_archive_bot.services.previews import plan_previews

        jobs = plan_previews(s, p, result.source_files)
        jobs = [PreviewJob(j.project_id, j.preview_id, j.source_key, j.source, j.previews_folder_id, requested_by=42) for j in jobs]
    await worker.start()
    assert await worker.enqueue(jobs) == 1
    assert await worker.enqueue(jobs) == 0  # de-duplicated while pending
    await asyncio.wait_for(worker.queue.join(), timeout=30)
    await worker.stop()
    with session_scope() as s:
        row = s.query(PreviewAsset).one()
        assert row.status == PreviewStatus.READY and row.preview_name == "Cross.mp4" and row.preview_drive_id
    assert "Preview ready" in harness.bot.last(42)["text"]
