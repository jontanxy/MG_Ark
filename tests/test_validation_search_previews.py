from __future__ import annotations


import pytest

from mg_archive_bot.constants import AssetCategory, PreviewStatus, ProjectStatus
from mg_archive_bot.db import session_scope
from mg_archive_bot.services import notifications
from mg_archive_bot.services import projects as project_service
from mg_archive_bot.services import search as search_service
from mg_archive_bot.services import users as user_service
from mg_archive_bot.services.drive import DriveError
from mg_archive_bot.services.previews import (
    PreviewJob,
    build_ffmpeg_command,
    plan_previews,
    process_preview,
    record_result,
    remove_orphans,
)
from mg_archive_bot.services.validation import collect_files, latest_report, validate_project
from mg_archive_bot.constants import build_folder_tree


async def make_project(s, drive, settings, name="Easter Opening 2026", **flags):
    p = project_service.create_draft(s, name, 1, "Lead", 2026)
    for cat, on in flags.items():
        project_service.set_declaration(s, p, AssetCategory(cat), on)
    await project_service.provision_folders(s, p, drive, settings)
    return p


@pytest.mark.asyncio
async def test_validation_only_checks_declared_assets(db, settings, drive):
    with session_scope() as s:
        p = await make_project(s, drive, settings, TIMELINE=False, CONTIN_VIDEOS=True, CONTIN_LYRICS=True, PSD=False)
        result = await validate_project(s, p, drive, settings)
        report = result.report
        assert not report.complete and (result.old_status, result.new_status) == (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE)
        required = {i.key for i in report.required_items}
        assert required == {"fonts", "ae", "contin_prores", "contin_hap", "lyrics_png"}
        assert {i.key for i in report.items if not i.required} == {"psd", "timeline_prores", "timeline_hap"}
        # upload everything except Contin Videos / Hap
        drive.put_file(p.folder("fonts").drive_id, "Font.otf")
        sub = drive.create_folder("AE Project", p.folder("ae").drive_id)  # files in sub-folders count
        drive.put_file(sub.id, "opening.aep")
        drive.put_file(p.folder("contin_prores").drive_id, "Clouds.mov", md5="a1", mime_type="video/quicktime")
        drive.put_file(p.folder("lyrics_png").drive_id, "lyric_01.png")
        result = await validate_project(s, p, drive, settings)
        assert [i.key for i in result.report.missing] == ["contin_hap"]
        assert result.source_files["contin_prores"][0].name == "Clouds.mov"
        assert p.status == ProjectStatus.INCOMPLETE
        drive.put_file(p.folder("contin_hap").drive_id, "Clouds_hap.mov")
        result = await validate_project(s, p, drive, settings)
        assert result.report.complete and result.became_ready and p.status == ProjectStatus.READY_FOR_VERIFICATION
        assert latest_report(s, p).complete
        # a folder that cannot be listed is 'unknown', not 'missing': status and stored report are untouched
        runs_before = len(s.query(__import__("mg_archive_bot.models", fromlist=["ValidationRun"]).ValidationRun).all())
        drive.delete(p.folder("fonts").drive_id)
        result = await validate_project(s, p, drive, settings)
        assert result.report.had_errors and [i.key for i in result.report.errored] == ["fonts"]
        assert result.report.missing == [] and not result.report.complete and not result.became_ready
        assert p.status == ProjectStatus.READY_FOR_VERIFICATION
        assert len(s.query(__import__("mg_archive_bot.models", fromlist=["ValidationRun"]).ValidationRun).all()) == runs_before
        assert latest_report(s, p).complete  # previous good report still stands


def test_collect_files_depth_limit(drive):
    root = drive.create_folder("R", drive.ROOT_ID)
    d1 = drive.create_folder("d1", root.id)
    d2 = drive.create_folder("d2", d1.id)
    d3 = drive.create_folder("d3", d2.id)
    drive.put_file(root.id, "a")
    drive.put_file(d1.id, "b")
    drive.put_file(d2.id, "c")
    drive.put_file(d3.id, "d")  # depth 3 → beyond limit
    drive.put_file(root.id, ".DS_Store")
    drive.put_file(root.id, "._a")
    drive.put_file(root.id, "Thumbs.db")
    drive.put_file(root.id, "empty.mov", size=0)
    assert sorted(f.name for f in collect_files(drive, root.id)) == ["a", "b", "c"]
    with pytest.raises(DriveError):
        collect_files(drive, "nope")


@pytest.mark.asyncio
async def test_search_and_ranking(db, settings, drive):
    with session_scope() as s:
        a = await make_project(s, drive, settings, "Easter Opening 2026")
        project_service.set_metadata_field(s, a, "tags", "worship, gold, particles")
        project_service.set_metadata_field(s, a, "event", "Easter Service")
        b = await make_project(s, drive, settings, "Gold Particles Loop")
        project_service.set_metadata_field(s, b, "description", "worship background")
        c = await make_project(s, drive, settings, "Christmas 2025")
        project_service.set_metadata_field(s, c, "colours", "gold, red")
        project_service.set_metadata_field(s, c, "tags", "worship")
        d = project_service.create_draft(s, "Draft Gold", 1, "Lead", 2026)  # drafts never appear
        project_service.set_metadata_field(s, d, "tags", "gold, worship")

        hits = search_service.search_projects(s, "worship, gold")
        assert [h.project.name for h in hits] == ["Easter Opening 2026", "Christmas 2025", "Gold Particles Loop"]
        assert hits[0].matched == {"worship": "tag", "gold": "tag"}
        assert hits[1].matched == {"worship": "tag", "gold": "metadata"}
        assert hits[2].matched == {"worship": "description", "gold": "name"}
        assert search_service.search_projects(s, "WORSHIP , gold , particles") and len(search_service.search_projects(s, "worship, gold, particles")) == 2
        assert search_service.search_projects(s, "worship, nothing-matches") == []
        assert search_service.search_projects(s, "") == []
        assert [h.project.name for h in search_service.search_projects(s, "easter service")] == ["Easter Opening 2026"]
        assert [h.project.name for h in search_service.search_projects(s, "2025")] == ["Christmas 2025"]
        # tier ordering: "gold, particles" → A (tag,tag) > B (name,name) even though B matches its name twice
        assert [h.project.name for h in search_service.search_projects(s, "gold, particles")] == ["Easter Opening 2026", "Gold Particles Loop"]
        # exact whole-name match wins ties within the same tier
        await make_project(s, drive, settings, "Gold")
        assert [h.project.name for h in search_service.search_projects(s, "gold")] == [
            "Easter Opening 2026",  # exact tag
            "Gold",  # name tier, whole-name match wins the tie
            "Gold Particles Loop",  # name tier
            "Christmas 2025",  # metadata (colours)
        ]
        card = notifications.search_result_card(a)
        assert "Easter Opening 2026" in card and "#worship" in card and "no previews yet" in card


def test_ffmpeg_command_shape(tmp_path):
    cmd = build_ffmpeg_command("ffmpeg", tmp_path / "in.mov", tmp_path / "out.mp4", 1280, 26)
    assert cmd[0] == "ffmpeg" and cmd[-1].endswith("out.mp4")
    assert "libx264" in cmd and "yuv420p" in cmd and "+faststart" in cmd and "scale=w='trunc(min(1280,iw)/2)*2':h=-2" in cmd[cmd.index("-vf") + 1]


@pytest.mark.asyncio
async def test_preview_planning_and_processing(db, settings, drive, fake_ffmpeg, monkeypatch):
    settings.ffmpeg_path = str(fake_ffmpeg)
    with session_scope() as s:
        p = await make_project(s, drive, settings, TIMELINE=True, CONTIN_VIDEOS=False)
        clouds = drive.put_file(p.folder("timeline_prores").drive_id, "Clouds.mov", content=b"x" * 10, md5="m1", mime_type="video/quicktime")
        drive.put_file(p.folder("timeline_prores").drive_id, "notes.txt", content=b"n")
        # Contin Videos is not declared → its ProRes files are ignored
        drive.put_file(p.folder("contin_prores").drive_id, "Ignored.mov", md5="zz", mime_type="video/quicktime")
        result = await validate_project(s, p, drive, settings)
        jobs = plan_previews(s, p, result.source_files)
        assert [j.source.name for j in jobs] == ["Clouds.mov"] and jobs[0].source_key == "timeline_prores"
        assert p.previews[0].status == PreviewStatus.PENDING
        # unchanged + pending → re-planned idempotently (same row), not duplicated
        assert len(plan_previews(s, p, result.source_files)) == 1 and len(p.previews) == 1
        job = jobs[0]
        preview_id = job.preview_id
    # crf 26 → 384,615 bytes from the stand-in ffmpeg; the MP4 goes to _Previews/ on Drive only
    res = process_preview(job, drive, settings, None)
    assert res.status == PreviewStatus.READY and res.size_bytes == 384_615 and res.preview_name == "Clouds.mp4"
    assert drive.get_file(res.preview_drive_id).name == "Clouds.mp4"
    assert drive.path_of(res.preview_drive_id).endswith("/_Previews/Clouds.mp4")
    assert not any(settings.work_dir.iterdir())  # work dir cleaned, nothing cached locally
    with session_scope() as s:
        row = record_result(s, preview_id, res)
        assert row.status == PreviewStatus.READY and row.preview_link == f"https://drive.google.com/file/d/{res.preview_drive_id}/view"
        p = project_service.get_project(s, row.project_id)
        assert len(p.ready_previews) == 1
        # unchanged source → nothing to do
        result = await validate_project(s, p, drive, settings)
        assert plan_previews(s, p, result.source_files) == []
        # changed source → re-queued
        drive.replace_content(clouds.id, md5="m2", modified_time="2026-02-02T00:00:00.000Z")
        result = await validate_project(s, p, drive, settings)
        jobs = plan_previews(s, p, result.source_files)
        assert len(jobs) == 1 and row.status == PreviewStatus.PENDING
        old_drive_id = row.preview_drive_id
    # re-encoding replaces the old Drive preview
    res = process_preview(jobs[0], drive, settings, old_drive_id)
    assert res.status == PreviewStatus.READY and drive.get_file(old_drive_id) is None and res.preview_drive_id
    res_drive_id = res.preview_drive_id
    # failure path
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    res = process_preview(jobs[0], drive, settings, None)
    assert res.status == PreviewStatus.FAILED and "boom" in res.error
    monkeypatch.delenv("FAKE_FFMPEG_FAIL")
    with session_scope() as s:
        record_result(s, preview_id, res)
        p = project_service.get_project(s, 1)
        assert p.previews[0].status == PreviewStatus.FAILED and p.ready_previews == []
        result = await validate_project(s, p, drive, settings)
        assert plan_previews(s, p, result.source_files) == []  # failed rows are not retried by scans
        assert len(plan_previews(s, p, result.source_files, force=True)) == 1  # ...but a manual generate retries
        assert p.previews[0].status == PreviewStatus.PENDING
    # missing ffmpeg is a clear error, not a crash
    settings.ffmpeg_path = "/nonexistent/ffmpeg"
    res = process_preview(jobs[0], drive, settings, None)
    assert res.status == PreviewStatus.FAILED and "ffmpeg not found" in res.error
    # source removed from Drive → row dropped and its Drive preview reported as an orphan
    with session_scope() as s:
        p = project_service.get_project(s, 1)
        p.previews[0].preview_drive_id = res_drive_id
        s.flush()
        drive.delete(clouds.id)
        result = await validate_project(s, p, drive, settings)
        orphans = []
        plan_previews(s, p, result.source_files, orphans=orphans)
        assert p.previews == [] and [o.preview_drive_id for o in orphans] == [res_drive_id]
    remove_orphans(orphans, drive)
    assert drive.get_file(res_drive_id) is None


def test_process_preview_never_uses_drive_name_as_path(db, settings, drive, fake_ffmpeg, tmp_path):
    settings.ffmpeg_path = str(fake_ffmpeg)
    previews = drive.create_folder("_Previews", drive.ROOT_ID)
    evil = drive.put_file(drive.ROOT_ID, "../../evil.mov", content=b"x", md5="e", mime_type="video/quicktime")
    absolute = drive.put_file(drive.ROOT_ID, f"{tmp_path}/abs.mov", content=b"x", md5="a", mime_type="video/quicktime")
    for src in (evil, absolute):
        job = PreviewJob(1, 1, "timeline_prores", src, previews.id)
        res = process_preview(job, drive, settings, None)
        assert res.status == PreviewStatus.READY and res.preview_name in ("evil.mp4", "abs.mp4")
        assert drive.get_file(res.preview_drive_id).parents == (previews.id,)
    assert not (settings.work_dir.parent.parent / "evil.mov").exists() and not (tmp_path / "abs.mov").exists()
    assert not any(settings.work_dir.iterdir())


def test_kill_active_transcodes(tmp_path, fake_ffmpeg, monkeypatch):
    import threading
    import time

    from mg_archive_bot.services.previews import PreviewError, _ACTIVE_PROCS, kill_active_transcodes, transcode

    monkeypatch.setenv("FAKE_FFMPEG_SLEEP", "30")
    errors: list[Exception] = []

    def run():
        try:
            transcode(str(fake_ffmpeg), tmp_path / "in.mov", tmp_path / "out.mp4", 1280, 26)
        except PreviewError as exc:
            errors.append(exc)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):
        if _ACTIVE_PROCS:
            break
        time.sleep(0.05)
    assert kill_active_transcodes() == 1
    t.join(timeout=10)
    assert errors and "stopped" in str(errors[0])


@pytest.mark.asyncio
async def test_notification_texts(db, settings, drive):
    tree = build_folder_tree(settings.folder_names())
    with session_scope() as s:
        user_service.register_designer(s, 11, "Alice", "alice")
        p = await make_project(s, drive, settings, TIMELINE=True)
        project_service.toggle_assignment(s, p, 11, AssetCategory.TIMELINE)
        text = notifications.announcement(p, tree)
        assert "New archive: Easter Opening 2026" in text
        assert 'href="tg://user?id=11">Alice</a>' in text
        assert "Timeline / ProRes 4444" in text and "Contin Videos" not in text and p.drive_link in text
        result = await validate_project(s, p, drive, settings)
        progress = notifications.progress_message(p, result.report, tree, __import__("zoneinfo").ZoneInfo("Asia/Singapore"))
        assert progress.count("❌") == 4 and "Alice" in progress
        reminder = notifications.reminder_message(p, result.report, tree)
        assert reminder.startswith("⏰") and "Alice" in reminder
        details = notifications.project_details(p, result.report, tree, __import__("zoneinfo").ZoneInfo("UTC"))
        assert "Declared assets:</b> Timeline" in details and "Previews:</b> 0" in details
