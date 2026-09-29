from __future__ import annotations

from datetime import timedelta

import pytest

from mg_archive_bot.constants import AssetCategory, ProjectStatus, Role, UserStatus, build_folder_tree
from mg_archive_bot.db import session_scope
from mg_archive_bot.security import generate_provisioning_token, hash_password, normalise_token, verify_password
from mg_archive_bot.services import groups as group_service
from mg_archive_bot.services import projects as project_service
from mg_archive_bot.services import users as user_service
from mg_archive_bot.services.projects import ProjectError
from mg_archive_bot.util import normalise_terms, utcnow
from tests.conftest import PASSWORD, SUPER_ADMIN_ID


def test_password_hashing_roundtrip():
    stored = hash_password("hunter22")
    assert stored.startswith("scrypt$")
    assert verify_password("hunter22", stored)
    assert not verify_password("hunter23", stored)
    assert not verify_password("", stored)
    assert not verify_password("x", None)
    assert not verify_password("x", "garbage")


def test_token_format_and_normalisation():
    token = generate_provisioning_token()
    assert len(token) == 12 and token.startswith("MG-") and token[7] == "-"
    assert normalise_token(token.lower()) == token
    assert normalise_token(" mg " + token[3:7] + token[8:] + " ") == token
    assert normalise_token("MG-ABC") == ""
    assert normalise_token("") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("worship, gold, particles", ["worship", "gold", "particles"]),
        ("  Worship ,GOLD, gold ,, ", ["worship", "gold"]),
        ("gold particles", ["gold", "particles"]),
        ("#worship, #gold", ["worship", "gold"]),
        ("", []),
        ("youth camp, 2026", ["youth camp", "2026"]),
    ],
)
def test_normalise_terms(raw, expected):
    assert normalise_terms(raw) == expected


def test_user_registration_roles_and_revocation(db, settings):
    with session_scope() as s:
        assert user_service.verify_access_password(s, PASSWORD)
        assert not user_service.verify_access_password(s, "wrong")
        u = user_service.register_designer(s, 5, "Dee Signer", "dee")
        assert u.role == Role.DESIGNER and u.status == UserStatus.ACTIVE
        admin = user_service.ensure_super_admin(s, SUPER_ADMIN_ID, "Boss", None)
        assert admin.role == Role.SUPER_ADMIN
        # roles
        user_service.set_role(s, 5, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        assert user_service.get_user(s, 5).role == Role.TEAM_LEAD
        user_service.set_role(s, 5, Role.LIGHTS, SUPER_ADMIN_ID)
        assert user_service.get_user(s, 5).role == Role.LIGHTS
        assert [u.telegram_id for u in user_service.list_assignable_users(s)] == [SUPER_ADMIN_ID]  # Lights excluded
        user_service.set_role(s, 5, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        with pytest.raises(user_service.UserError):
            user_service.set_role(s, SUPER_ADMIN_ID, Role.DESIGNER, SUPER_ADMIN_ID)
        with pytest.raises(user_service.UserError):
            user_service.set_role(s, 5, Role.SUPER_ADMIN, SUPER_ADMIN_ID)
        # revoke / re-register blocked / restore
        user_service.revoke_user(s, 5, SUPER_ADMIN_ID)
        with pytest.raises(user_service.UserError):
            user_service.register_designer(s, 5, "Dee", "dee")
        with pytest.raises(user_service.UserError):
            user_service.revoke_user(s, SUPER_ADMIN_ID, SUPER_ADMIN_ID)
        user_service.restore_user(s, 5)
        assert user_service.get_user(s, 5).status == UserStatus.ACTIVE
        # super admin re-ensured even if someone tampered
        admin.role = Role.DESIGNER
        s.flush()
        assert user_service.ensure_super_admin(s, SUPER_ADMIN_ID, "Boss", None).role == Role.SUPER_ADMIN
        # a stray SUPER_ADMIN row (DB tampering) is demoted at startup
        user_service.get_user(s, 5).role = Role.SUPER_ADMIN
        s.flush()
        assert user_service.reconcile_super_admin(s, SUPER_ADMIN_ID) == [5]
        assert user_service.get_user(s, 5).role == Role.TEAM_LEAD


def test_login_lockout(db, settings):
    with session_scope() as s:
        for i in range(1, 5):
            failures, lock = user_service.record_failed_login(s, 7, 5, 15)
            assert (failures, lock) == (i, 0)
        failures, lock = user_service.record_failed_login(s, 7, 5, 15)
        assert lock == 15 * 60
        assert user_service.lock_remaining_seconds(s, 7) > 0
        user_service.clear_login_attempts(s, 7)
        assert user_service.lock_remaining_seconds(s, 7) == 0


def test_password_change_rules(db):
    with session_scope() as s:
        with pytest.raises(user_service.UserError):
            user_service.set_access_password(s, "short")
        with pytest.raises(user_service.UserError):
            user_service.set_access_password(s, "change-me-please")  # the .env.example placeholder
        with pytest.raises(user_service.UserError):
            user_service.set_access_password(s, "aaaaaaaaaaaaaaaa")  # too repetitive
        user_service.set_access_password(s, "new-password-9!!")
        assert user_service.verify_access_password(s, "new-password-9!!")
        assert not user_service.verify_access_password(s, PASSWORD)
        assert user_service.seed_password_if_missing(s, "ignored") is False


def test_provisioning_tokens_and_groups(db):
    with session_scope() as s:
        t1 = group_service.create_token(s, 42, 24)
        t2 = group_service.create_token(s, 42, 24)  # supersedes t1
        assert group_service.find_valid_token(s, t1.token) is None
        assert group_service.find_valid_token(s, t2.token.lower()) is not None
        assert [t.token for t in group_service.pending_tokens_for(s, 42)] == [t2.token]
        group = group_service.authorise_group(s, -100123, "MG Team", t2)
        assert group.is_active and group.created_by == 42
        assert group_service.find_valid_token(s, t2.token) is None  # single use
        assert group_service.is_group_authorised(s, -100123)
        with pytest.raises(group_service.GroupError):
            group_service.authorise_group(s, -100999, "Other", t2)
        # expiry
        t3 = group_service.create_token(s, 42, 24)
        t3.expires_at = utcnow() - timedelta(seconds=1)
        s.flush()
        assert group_service.find_valid_token(s, t3.token) is None
        group_service.revoke_group(s, -100123, revoked_by=SUPER_ADMIN_ID)
        assert not group_service.is_group_authorised(s, -100123)
        # admin-revoked groups cannot be re-activated with a Team Lead token; restore first
        t4 = group_service.create_token(s, 42, 24)
        with pytest.raises(group_service.GroupError):
            group_service.authorise_group(s, -100123, "MG Team", t4)
        assert group_service.find_valid_token(s, t4.token) is not None  # token not consumed
        group_service.restore_group(s, -100123)
        assert group_service.is_group_authorised(s, -100123)
        # bot kicked (revoked_by=None) → a fresh token re-activates
        group_service.revoke_group(s, -100123, revoked_by=None)
        group_service.authorise_group(s, -100123, "MG Team", t4)
        assert group_service.is_group_authorised(s, -100123)
        # migration carries authorisation + project links to the new chat id
        p = project_service.create_draft(s, "Migrating", 1, "Lead", 2026)
        project_service.set_group(s, p, -100123)
        assert group_service.migrate_group(s, -100123, -100777) is True
        assert group_service.is_group_authorised(s, -100777) and not group_service.get_group(s, -100123)
        assert p.mg_group_chat_id == -100777
        assert group_service.migrate_group(s, -1, -2) is False
        # unauthorised chat bookkeeping
        group_service.note_unauthorised_chat(s, -5, "Random")
        assert group_service.stale_unauthorised_chats(s, 0)
        group_service.forget_unauthorised_chat(s, -5)
        assert not group_service.stale_unauthorised_chats(s, 0)


def test_project_draft_declarations_metadata_assignments(db):
    with session_scope() as s:
        user_service.register_designer(s, 11, "Alice", "alice")
        user_service.register_designer(s, 12, "Bob", None)
        p = project_service.create_draft(s, "  Easter   Opening 2026 ", 1, "Lead", 2026)
        assert p.name == "Easter Opening 2026" and p.status == ProjectStatus.DRAFT and p.year == 2026
        with pytest.raises(ProjectError):
            project_service.create_draft(s, "x", 1, "Lead", 2026)
        assert project_service.toggle_declaration(s, p, AssetCategory.TIMELINE) is True
        project_service.set_declaration(s, p, AssetCategory.PSD, True)
        assert p.asset_types == "Working Files, Timeline, PSD"
        assert project_service.declared_categories(p) == [AssetCategory.TIMELINE, AssetCategory.PSD]
        assert project_service.set_metadata_field(s, p, "tags", "Worship, gold, GOLD, particles") == "worship, gold, particles"
        assert p.tag_names == ["gold", "particles", "worship"]
        with pytest.raises(ProjectError):
            project_service.set_metadata_field(s, p, "year", "abc")
        assert project_service.set_metadata_field(s, p, "year", "2025") == "2025"
        assert project_service.set_metadata_field(s, p, "event", "  Easter  Service ") == "Easter Service"
        assert project_service.set_metadata_field(s, p, "event", "-") == ""
        assert project_service.toggle_assignment(s, p, 11, AssetCategory.ALL) is True
        assert project_service.toggle_assignment(s, p, 12, AssetCategory.TIMELINE) is True
        assert [u.telegram_id for u in project_service.assignees_for(p, AssetCategory.TIMELINE)] == [11, 12]
        assert [u.telegram_id for u in project_service.assignees_for(p, AssetCategory.CONTIN_VIDEOS)] == [11]
        assert project_service.toggle_assignment(s, p, 11, AssetCategory.ALL) is False
        assert [u.telegram_id for u in project_service.assignees_for(p, None)] == [12]
        with pytest.raises(ProjectError):
            project_service.toggle_assignment(s, p, 999, AssetCategory.ALL)


@pytest.mark.asyncio
async def test_provision_folders_creates_exact_tree(db, settings, drive):
    with session_scope() as s:
        p = project_service.create_draft(s, "Youth Camp 2026", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.CONTIN_VIDEOS, True)
        await project_service.provision_folders(s, p, drive, settings)
        assert p.status == ProjectStatus.ACTIVE and p.drive_root_id and p.drive_link.endswith(p.drive_root_id)
        paths = sorted(drive.path_of(f.drive_id) for f in p.folders)
    assert paths == sorted(
        [
            "Archive Root/Youth Camp 2026",
            "Archive Root/Youth Camp 2026/Working File",
            "Archive Root/Youth Camp 2026/Working File/Fonts",
            "Archive Root/Youth Camp 2026/Working File/AE",
            "Archive Root/Youth Camp 2026/Final Render",
            "Archive Root/Youth Camp 2026/Final Render/Timeline",
            "Archive Root/Youth Camp 2026/Final Render/Timeline/ProRes 4444",
            "Archive Root/Youth Camp 2026/Final Render/Timeline/Hap/Hap Alpha",
            "Archive Root/Youth Camp 2026/Final Render/Contin Videos",
            "Archive Root/Youth Camp 2026/Final Render/Contin Videos/ProRes 4444",
            "Archive Root/Youth Camp 2026/Final Render/Contin Videos/Hap/Hap Alpha",
            "Archive Root/Youth Camp 2026/Final Render/Contin Lyrics",
            "Archive Root/Youth Camp 2026/Final Render/Contin Lyrics/PNG",
            "Archive Root/Youth Camp 2026/_Previews",
        ]
    )
    # PSD is the only optional folder: absent until declared, then created lazily.
    with session_scope() as s:
        p = project_service.get_project(s, 1)
        assert p.folder("psd") is None
        project_service.set_declaration(s, p, AssetCategory.PSD, True)
        assert await project_service.ensure_folders(s, p, drive, settings) == ["psd"]
        assert drive.path_of(p.folder("psd").drive_id) == "Archive Root/Youth Camp 2026/Working File/PSD"
        assert await project_service.ensure_folders(s, p, drive, settings) == []
        # duplicate name on Drive gets a suffix but the bot refuses duplicate active names
        with pytest.raises(ProjectError):
            project_service.create_draft(s, "youth camp 2026", 1, "Lead", 2026)
        assert project_service._unique_root_name(drive, "Youth Camp 2026", drive.ROOT_ID) == "Youth Camp 2026 (2)"
        with pytest.raises(ProjectError):
            await project_service.provision_folders(s, p, drive, settings)


def test_state_transitions(db):
    with session_scope() as s:
        p = project_service.create_draft(s, "State Machine", 1, "Lead", 2026)
        p.status = ProjectStatus.ACTIVE
        assert project_service.apply_validation_outcome(s, p, False) == (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE)
        assert project_service.apply_validation_outcome(s, p, True) == (ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION)
        assert project_service.apply_validation_outcome(s, p, False) == (ProjectStatus.READY_FOR_VERIFICATION, ProjectStatus.INCOMPLETE)
        with pytest.raises(ProjectError):
            project_service.verify_project(s, p, 1)
        project_service.apply_validation_outcome(s, p, True)
        project_service.verify_project(s, p, 1)
        assert p.status == ProjectStatus.ARCHIVED and p.verified_by == 1
        assert project_service.apply_validation_outcome(s, p, False) == (ProjectStatus.ARCHIVED, ProjectStatus.ARCHIVED)
        project_service.reopen_project(s, p)
        assert p.status == ProjectStatus.ACTIVE and p.verified_by is None
        with pytest.raises(ProjectError):
            project_service.reopen_project(s, p)


@pytest.mark.asyncio
async def test_collection_service(db, settings, drive):
    from mg_archive_bot.services import collections as collection_service

    with session_scope() as s:
        bf = collection_service.create_collection(s, "  BF ", 1)
        assert bf.name == "BF" and bf.drive_id is None
        assert collection_service.create_collection(s, "bf", 2) is bf  # case-insensitive re-use
        with pytest.raises(ProjectError):
            collection_service.create_collection(s, "x", 1)
        await collection_service.ensure_collection_folder(s, bf, drive, settings)
        assert drive.path_of(bf.drive_id) == "Archive Root/BF" and bf.link.endswith(bf.drive_id)
        first_id = bf.drive_id
        await collection_service.ensure_collection_folder(s, bf, drive, settings)
        assert bf.drive_id == first_id  # idempotent
        p = project_service.create_draft(s, "Opening", 1, "Lead", 2026, bf)
        assert p.collection == "BF" and p.full_name == "BF / Opening"
        await project_service.provision_folders(s, p, drive, settings)
        assert drive.path_of(p.drive_root_id) == "Archive Root/BF/Opening"
        assert project_service.name_in_use(s, "opening", collection_id=bf.id)
        assert not project_service.name_in_use(s, "opening")  # top level is a different namespace
        assert [c.name for c in collection_service.list_collections(s)] == ["BF"]


def test_schema_upgrade_adds_missing_columns(tmp_path):
    """A database created by an older version gains new columns on startup instead of crashing."""
    import sqlite3

    from mg_archive_bot.db import init_db

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, name VARCHAR(200), status VARCHAR(40), has_timeline BOOLEAN, "
        "has_contin_videos BOOLEAN, has_contin_lyrics BOOLEAN, has_psd BOOLEAN, collection VARCHAR(200), description TEXT, "
        "event VARCHAR(200), ministry VARCHAR(200), style VARCHAR(200), colours VARCHAR(200), year INTEGER, creator VARCHAR(200), "
        "asset_types VARCHAR(200), created_by INTEGER, mg_group_chat_id INTEGER, drive_root_id VARCHAR(128), drive_link VARCHAR(512), "
        "created_at DATETIME, updated_at DATETIME, activated_at DATETIME, last_validated_at DATETIME, last_complete BOOLEAN, "
        "last_reminder_at DATETIME, verified_by INTEGER, verified_at DATETIME);"
        "INSERT INTO projects (id, name, status, created_by, last_complete, updated_at) "
        "VALUES (1, 'Old', 'ACTIVE', 1, 0, '2025-01-01 00:00:00.000000');"
    )
    conn.commit()
    conn.close()
    engine = init_db(f"sqlite:///{path}")
    # a flag added by a newer version is a real "No" on old rows, not NULL; columns without a default stay empty
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT has_titlebars, lead_id, has_psd FROM projects WHERE id = 1").fetchone() == (0, None, None)
    # the value comes from the column itself (one statement, nothing to repeat after a crash), so no row was
    # rewritten: the last-modified time of old projects survives the upgrade
    assert conn.execute("SELECT updated_at FROM projects WHERE id = 1").fetchone() == ("2025-01-01 00:00:00.000000",)
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(projects)")}
    assert info["has_titlebars"][4] == "0" and info["lead_id"][4] is None
    # a row written by the previous version of the bot (which does not know the column) reads as "No" as well
    conn.execute("INSERT INTO projects (id, name, status, created_by, last_complete) VALUES (2, 'Older code', 'ACTIVE', 1, 0)")
    conn.commit()
    assert conn.execute("SELECT has_titlebars FROM projects WHERE id = 2").fetchone() == (0,)
    conn.close()
    from sqlalchemy import Column, String, Text

    from mg_archive_bot.db import Base, upgrade_schema

    assert upgrade_schema(engine) == []  # a second start changes nothing
    # defaults are written into the statement literally, whatever they contain
    table = Base.metadata.tables["projects"]
    tricky = {"probe_json": (Text, '{"complete":false, "n": :x}'), "probe_text": (String(80), "it's 100% a \\:b ? %s")}
    try:
        for name, (kind, default) in tricky.items():
            table.append_column(Column(name, kind, default=default))
        assert upgrade_schema(engine) == [f"projects.{name}" for name in tricky]
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT probe_json, probe_text FROM projects WHERE id = 1").fetchone() == tuple(d for _, d in tricky.values())
        conn.close()
    finally:
        for name in tricky:
            table._columns.remove(table.c[name])
    with session_scope() as s:
        p = project_service.get_project(s, 1)
        assert p.name == "Old" and p.collection_id is None and p.full_name == "Old"
        assert p.has_titlebars is False and project_service.declared_categories(p) == []
        assert project_service.toggle_declaration(s, p, AssetCategory.TITLEBARS) is True and p.asset_types == "Working Files, Titlebars"
        from mg_archive_bot.services import collections as collection_service

        bf = collection_service.create_collection(s, "BF", 1)
        p.collection_id = bf.id
        s.flush()
        s.refresh(p)
        assert project_service.get_project(s, 1).full_name == "BF / Old"
    engine.dispose()


@pytest.mark.asyncio
async def test_tracking_sheet_service(db, settings, drive):
    from zoneinfo import ZoneInfo

    from mg_archive_bot.services import tracking
    from mg_archive_bot.services.sheets import InMemorySheetsClient

    sheets = InMemorySheetsClient()
    tz = ZoneInfo("Asia/Singapore")
    with session_scope() as s:
        user_service.register_designer(s, 11, "Alice", None)
        sheet_id, url = tracking.ensure_sheet(s, drive, sheets, settings)
        assert url == f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        assert drive.get_file(sheet_id).name == "MG Archive Index" and drive.path_of(sheet_id) == "Archive Root/MG Archive Index"
        assert tracking.ensure_sheet(s, drive, sheets, settings) == (sheet_id, url)  # remembered, not re-created
        assert sheets.get_values(sheet_id, f"'Projects'!A1:{tracking.LAST_COL}1")[0] == tracking.HEADERS
        col = tracking.HEADERS.index
        assert any("frozenRowCount" in str(r) for r in sheets.requests) and any("setBasicFilter" in r for r in sheets.requests)
        p = project_service.create_draft(s, "Opening", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.TIMELINE, True)
        project_service.set_metadata_field(s, p, "tags", "worship, gold")
        project_service.toggle_assignment(s, p, 11, AssetCategory.ALL)
        await project_service.provision_folders(s, p, drive, settings)
        row = tracking.build_row(s, p, tz)
        assert row[:6] == [str(p.id), "", "Opening", "Active", "", "2026"] and row[10] == "gold, worship" and row[12] == "Yes" and row[13] == "No"
        assert row[col("Titlebars")] == "No" and row[col("PSD")] == "No" and len(row) == len(tracking.HEADERS)
        assert row[col("Assigned")] == "Alice" and row[col("Drive link")] == p.drive_link
        assert tracking.upsert_row(sheets, sheet_id, row) == "appended"
        p.status = ProjectStatus.ARCHIVED
        p.verified_by, p.verified_at = 1, p.created_at
        s.flush()
        assert tracking.upsert_row(sheets, sheet_id, tracking.build_row(s, p, tz)) == "updated"
        values = sheets.get_values(sheet_id, f"'Projects'!A:{tracking.LAST_COL}")
        assert len(values) == 2 and values[1][3] == "Archived" and values[1][col("Archived")] != ""
        # rebuild rewrites everything from the database (drafts excluded)
        project_service.create_draft(s, "Draft only", 1, "Lead", 2026)
        assert tracking.rebuild(sheets, sheet_id, tracking.all_rows(s, tz)) == 1
        assert [r[2] for r in sheets.get_values(sheet_id, f"'Projects'!A:{tracking.LAST_COL}")[1:]] == ["Opening"]


@pytest.mark.asyncio
async def test_file_listing_tree_and_rendering(db, settings, drive):
    from mg_archive_bot.services.drive import DriveError
    from mg_archive_bot.services.listing import build_listing, render_listing

    with session_scope() as s:
        p = project_service.create_draft(s, "Listing", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.TIMELINE, True)
        await project_service.provision_folders(s, p, drive, settings)
        f = {x.key: x.drive_id for x in p.folders}
        known = {x.drive_id: x.key for x in p.folders}
        root_id, name = p.drive_root_id, p.name
    order = {spec.key: i for i, spec in enumerate(build_folder_tree(settings.folder_names()))}
    drive.put_file(f["fonts"], "Gotham-Book.otf", size=118_000)
    drive.put_file(f["fonts"], "Gotham-Bold.otf", size=120_000)
    drive.put_file(f["fonts"], ".DS_Store", size=6_000)  # junk is hidden
    sub = drive.create_folder("Assets", f["ae"])
    drive.put_file(sub.id, "logo <v2>.png", size=5_000)
    drive.put_file(f["ae"], "opening.aep", size=2_000_000)
    drive.put_file(f["timeline_prores"], "Loop.mov", size=8_400_000_000)
    root = build_listing(drive, root_id, name, known, order)
    assert [x.name for x in root.folders] == ["Working File", "Final Render", "_Previews"]  # archive order, not alphabetical
    working = root.folders[0]
    assert [x.name for x in working.folders] == ["Fonts", "AE"] and working.total_files == 4
    assert [x.name for x in working.folders[0].files] == ["Gotham-Bold.otf", "Gotham-Book.otf"]  # files alphabetical
    assert root.total_files == 5 and root.total_size == 118_000 + 120_000 + 5_000 + 2_000_000 + 8_400_000_000
    chunks = render_listing("Listing", root)
    assert len(chunks) == 1
    text = chunks[0]
    assert text.startswith("📂 <b>Listing</b> — 5 files · 7.8 GB\n<a href=\"https://drive.google.com/drive/folders/")
    assert "logo &lt;v2&gt;.png · 4.9 KB" in text and ".DS_Store" not in text and "Loop.mov · 7.8 GB" in text
    assert "Hap/Hap Alpha</a> — empty" in text and "Fonts</a> (2)" in text and "AE</a> (2)" in text
    assert text.index("Working File") < text.index("Final Render") < text.index("_Previews")
    assert "<b><a href=" in text  # top-level folders are bold links
    # an unreadable folder is reported instead of aborting the whole listing
    original = drive.list_children

    def flaky(folder_id):
        if folder_id == f["timeline"]:
            raise DriveError("boom")
        return original(folder_id)

    drive.list_children = flaky  # type: ignore[method-assign]
    text = render_listing("Listing", build_listing(drive, root_id, name, known, order))[0]
    assert "Timeline</a> — ⚠️ could not read" in text and "Loop.mov" not in text and "Gotham-Bold.otf" in text
    drive.list_children = original  # type: ignore[method-assign]
    # deep trees stop at the depth limit
    deep = f["ae"]
    for i in range(8):
        deep = drive.create_folder(f"level{i}", deep).id
    drive.put_file(deep, "buried.txt", size=1)
    text = render_listing("Listing", build_listing(drive, root_id, name, known, order))[0]
    assert "level3" in text and "buried.txt" not in text and "not everything is shown" in text
    # long listings are split into numbered messages
    for i in range(400):
        drive.put_file(f["lyrics_png"], f"lyric_{i:03d}.png", size=1_000)
    chunks = render_listing("Listing", build_listing(drive, root_id, name, known, order))
    assert len(chunks) > 1 and all(len(c) <= 3600 for c in chunks)
    assert chunks[0].endswith(f"<i>(1/{len(chunks)})</i>") and chunks[-1].endswith(f"<i>({len(chunks)}/{len(chunks)})</i>")
    assert "Open project folder" in chunks[0] and "Open project folder" not in chunks[1]
    # an empty project
    with session_scope() as s:
        q = project_service.create_draft(s, "Empty", 1, "Lead", 2026)
        await project_service.provision_folders(s, q, drive, settings)
        empty = build_listing(drive, q.drive_root_id, q.name, {x.drive_id: x.key for x in q.folders}, order)
    text = render_listing("Empty", empty)[0]
    assert text.startswith("📂 <b>Empty</b> — no files yet") and text.count("— empty") == 13  # every folder of a fresh tree


def test_reset_password_tool(db, settings, capsys):
    from mg_archive_bot.tools import reset_password

    with session_scope() as s:
        user_service.register_designer(s, 5, "Dee", None)
        for _ in range(5):
            user_service.record_failed_login(s, 77, 5, 15)  # someone is locked out
        assert user_service.lock_remaining_seconds(s, 77) > 0
    settings.initial_access_password = "brand-new-pass-2026!!"
    assert reset_password.main([], settings=settings) == 0
    assert "lockouts cleared" in capsys.readouterr().out
    with session_scope() as s:
        assert user_service.verify_access_password(s, "brand-new-pass-2026!!")
        assert not user_service.verify_access_password(s, PASSWORD)
        assert user_service.lock_remaining_seconds(s, 77) == 0
        assert user_service.get_user(s, 5).status == UserStatus.ACTIVE  # existing users untouched
    settings.initial_access_password = "short"
    assert reset_password.main([], settings=settings) == 1  # rejected, nothing changed
    settings.initial_access_password = ""
    assert reset_password.main([], settings=settings) == 2
    with session_scope() as s:
        assert user_service.verify_access_password(s, "brand-new-pass-2026!!")


def test_lead_backfill_and_release(db):
    with session_scope() as s:
        user_service.register_designer(s, 21, "Lead One", None)
        user_service.set_role(s, 21, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        user_service.ensure_super_admin(s, SUPER_ADMIN_ID, "Boss", None)
        led = project_service.create_draft(s, "Led By Lead", 21, "Lead One", 2026)
        by_admin = project_service.create_draft(s, "Made By Admin", SUPER_ADMIN_ID, "Boss", 2026)
        assert led.lead_id == 21 and by_admin.lead_id is None
        for p in (led, by_admin):
            p.status = ProjectStatus.ACTIVE
            p.lead_id = None
        s.flush()
        adopted, missing = project_service.backfill_leads(s)
        assert adopted == 1 and missing == ["Made By Admin"]
        with pytest.raises(ProjectError):
            project_service.set_lead(s, by_admin, SUPER_ADMIN_ID)
        project_service.set_lead(s, by_admin, 21)
        assert [p.name for p in project_service.release_led_projects(s, 21)] == ["Led By Lead", "Made By Admin"]
        assert led.lead_id is None and by_admin.lead_id is None


def test_memory_database_rejected():
    from mg_archive_bot.db import make_engine

    with pytest.raises(ValueError):
        make_engine("sqlite:///:memory:")


def test_resolve_tz_prefers_iana_zones(monkeypatch):
    from zoneinfo import ZoneInfo

    from mg_archive_bot.util import resolve_tz

    assert resolve_tz("Europe/London") == ZoneInfo("Europe/London")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert resolve_tz("") == ZoneInfo("Asia/Tokyo")


def test_folder_tree_names_overridable():
    tree = build_folder_tree({"hap": "Hap - Hap Alpha", "previews": ""})
    by_key = {f.key: f for f in tree}
    assert by_key["timeline_hap"].name == "Hap - Hap Alpha"
    assert by_key["previews"].name == "_Previews"
    assert [f.key for f in tree if f.is_leaf] == [
        "fonts", "ae", "psd", "timeline_prores", "timeline_hap", "contin_prores", "contin_hap", "lyrics_png",
        "titlebars",
    ]
    assert [f.key for f in tree if f.key.startswith("titlebars")] == ["titlebars"]  # one folder, no format folders
    assert by_key["titlebars"].parent_key == "final_render" and by_key["titlebars"].create_when == "has_titlebars"
    assert build_folder_tree({"titlebars": "Title Bars"})[[f.key for f in tree].index("titlebars")].name == "Title Bars"


def test_sheet_column_letters():
    from mg_archive_bot.services import tracking

    letters = {1: "A", 2: "B", 26: "Z", 27: "AA", 28: "AB", 52: "AZ", 53: "BA", 702: "ZZ", 703: "AAA"}
    assert {i: tracking.column_letter(i) for i in letters} == letters
    with pytest.raises(ValueError):
        tracking.column_letter(0)
    assert tracking.LAST_COL == tracking.column_letter(len(tracking.HEADERS)) and tracking.LAST_COL.isalpha()
    assert len(set(tracking.HEADERS)) == len(tracking.HEADERS) and "Titlebars" in tracking.HEADERS


@pytest.mark.asyncio
async def test_titlebars_declaration_creates_its_folder(db, settings, drive):
    with session_scope() as s:
        plain = project_service.create_draft(s, "No titlebars", 1, "Lead", 2026)
        project_service.set_declaration(s, plain, AssetCategory.TIMELINE, True)
        await project_service.provision_folders(s, plain, drive, settings)
        assert plain.has_titlebars is False
        assert not [f.key for f in plain.folders if f.key.startswith("titlebars")]
        final_render = [f.name for f in drive.list_children(plain.folder("final_render").drive_id)]
        assert sorted(final_render) == ["Contin Lyrics", "Contin Videos", "Timeline"]

        p = project_service.create_draft(s, "With titlebars", 1, "Lead", 2026)
        for category in (AssetCategory.PSD, AssetCategory.TITLEBARS, AssetCategory.TIMELINE):
            project_service.set_declaration(s, p, category, True)
        assert project_service.declared_categories(p) == [AssetCategory.TIMELINE, AssetCategory.TITLEBARS, AssetCategory.PSD]
        assert p.asset_types == "Working Files, Timeline, Titlebars, PSD"
        await project_service.provision_folders(s, p, drive, settings)
        assert drive.path_of(p.folder("titlebars").drive_id) == "Archive Root/With titlebars/Final Render/Titlebars"
        assert drive.list_children(p.folder("titlebars").drive_id) == []  # just the folder, nothing inside it
        assert sorted(f.key for f in p.folders if f.key.startswith("titlebars")) == ["titlebars"]
        required = [spec.key for spec in project_service.required_leaves(p, build_folder_tree(settings.folder_names()))]
        assert required == ["fonts", "ae", "psd", "timeline_prores", "timeline_hap", "titlebars"]

        # declaring it later on the older project adds that one folder, once
        project_service.set_declaration(s, plain, AssetCategory.TITLEBARS, True)
        assert await project_service.ensure_folders(s, plain, drive, settings) == ["titlebars"]
        assert await project_service.ensure_folders(s, plain, drive, settings) == []
        assert drive.path_of(plain.folder("titlebars").drive_id) == "Archive Root/No titlebars/Final Render/Titlebars"
        # switching it off keeps the folder (files are never removed) but nothing is required any more
        project_service.set_declaration(s, plain, AssetCategory.TITLEBARS, False)
        assert plain.folder("titlebars") is not None and plain.asset_types == "Working Files, Timeline"
        assert [k.key for k in project_service.required_leaves(plain, build_folder_tree())] == ["fonts", "ae", "timeline_prores", "timeline_hap"]


@pytest.mark.asyncio
async def test_format_folders_inside_titlebars_are_forgotten_not_removed(db, settings, drive):
    """For one evening Titlebars was split into ProRes 4444 and Hap/Hap Alpha. The bot stops tracking those two
    folders; what happens to them on Google Drive is for a person to decide."""
    from mg_archive_bot.models import ProjectFolder
    from mg_archive_bot.services.drive import folder_link
    from mg_archive_bot.services.validation import validate_project

    with session_scope() as s:
        p = project_service.create_draft(s, "Titles", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.TITLEBARS, True)
        await project_service.provision_folders(s, p, drive, settings)
        s.commit()
        titlebars = p.folder("titlebars").drive_id
        records_before = sorted(f.key for f in p.folders)
        assert project_service.forget_obsolete_folders(s, p) == []  # nothing to do for a project of today

        made = {}
        for key, name in (("titlebars_prores", "ProRes 4444"), ("titlebars_hap", "Hap/Hap Alpha")):
            folder = drive.create_folder(name, titlebars)
            p.folders.append(ProjectFolder(project_id=p.id, key=key, name=name, drive_id=folder.id, link=folder_link(folder.id)))
            made[key] = folder.id
        s.commit()
        calls = len(drive.calls)
        forgotten = project_service.forget_obsolete_folders(s, p)
        s.commit()
        assert forgotten == [
            ("titlebars_prores", "ProRes 4444", folder_link(made["titlebars_prores"])),
            ("titlebars_hap", "Hap/Hap Alpha", folder_link(made["titlebars_hap"])),
        ]
        assert sorted(f.key for f in p.folders) == records_before  # only the two are gone from the records
        # Google Drive was not asked to do anything: both folders are where they were, empty or not
        assert len(drive.calls) == calls
        assert sorted(f.name for f in drive.list_children(titlebars)) == ["Hap/Hap Alpha", "ProRes 4444"]
        assert not any(drive.get_file(i).trashed for i in made.values())
        assert project_service.forget_obsolete_folders(s, p) == []

        # a file that arrives in one of them LATER (an upload that was still running) counts for Titlebars
        drive.put_file(p.folder("fonts").drive_id, "Font.otf")
        drive.put_file(p.folder("ae").drive_id, "titles.aep")
        result = await validate_project(s, p, drive, settings)
        assert [i.key for i in result.report.missing] == ["titlebars"]
        late = drive.put_file(made["titlebars_prores"], "Titlebars_Song_A.mov")
        result = await validate_project(s, p, drive, settings)
        assert result.report.complete and {i.key: i.file_count for i in result.report.required_items}["titlebars"] == 1
        assert drive.get_file(late.id).trashed is False
        # ... also one folder further down; below that the check does not look (as for every other folder)
        sub = drive.create_folder("Song B", made["titlebars_hap"])
        drive.put_file(sub.id, "Titlebars_Song_B.mov")
        deeper = drive.create_folder("v2", sub.id)
        drive.put_file(deeper.id, "Titlebars_Song_B_v2.mov")
        result = await validate_project(s, p, drive, settings)
        assert {i.key: i.file_count for i in result.report.required_items}["titlebars"] == 2


def test_fake_sheet_enforces_the_grid_like_google():
    from mg_archive_bot.services.sheets import InMemorySheetsClient, SheetsError

    sheets = InMemorySheetsClient()
    assert sheets.sheet_grids("book") == {"Sheet1": (0, 26)}
    sheets.update_values("book", "'Sheet1'!A1:Z1", [["x"] * 26])
    for call in (
        lambda: sheets.update_values("book", "'Sheet1'!A1:AA1", [["x"] * 27]),
        lambda: sheets.update_values("book", "'Sheet1'!A2:Z2", [["x"] * 27]),
        lambda: sheets.get_values("book", "'Sheet1'!A1:AA1"),
        lambda: sheets.append_values("book", "'Sheet1'!A:AA", [["x"]]),
        lambda: sheets.clear_values("book", "'Sheet1'!A2:AA"),
        lambda: sheets.batch_update("book", [{"autoResizeDimensions": {"dimensions": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 27}}}]),
        lambda: sheets.batch_update("book", [{"setBasicFilter": {"filter": {"range": {"sheetId": 0, "endColumnIndex": 27}}}}]),
    ):
        with pytest.raises(SheetsError):
            call()
    sheets.batch_update("book", [{"appendDimension": {"sheetId": 0, "dimension": "COLUMNS", "length": 1}}])
    sheets.batch_update("book", [{"updateSheetProperties": {"properties": {"sheetId": 0, "title": "Projects"}, "fields": "title"}}])
    assert sheets.sheet_grids("book") == {"Projects": (0, 27)}  # the width follows the sheet through a rename
    sheets.update_values("book", "'Projects'!A1:AA1", [["x"] * 27])
    assert len(sheets.get_values("book", "'Projects'!A1:AA1")[0]) == 27


def _as_read(table: list[list[str]]) -> list[list[str]]:
    """What a read returns for a table: Google leaves out empty cells at the end of a row."""
    out = []
    for row in table:
        row = list(row)
        while row and row[-1] == "":
            row.pop()
        out.append(row)
    return out


class _CountingSheets:
    """Wraps the in-memory sheet: counts calls and can make the k-th call fail, before or after it is applied."""

    METHODS = ("sheet_grids", "get_values", "update_values", "append_values", "clear_values", "batch_update")

    def __init__(self, inner, fail_at: int | None = None, lost_answer: bool = False):
        self.inner, self.fail_at, self.lost_answer = inner, fail_at, lost_answer
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if name not in self.METHODS:
            return target

        def call(*args, **kwargs):
            from mg_archive_bot.services.sheets import SheetsError

            self.calls.append((name, args))
            failing = self.fail_at is not None and len(self.calls) == self.fail_at
            if failing and not self.lost_answer:
                raise SheetsError("Google Sheets error: backend unavailable")
            result = target(*args, **kwargs)
            if failing:
                raise SheetsError("Google Sheets error: the answer was lost")
            return result

        return call


@pytest.mark.asyncio
async def test_index_sheet_from_an_older_version_gains_the_new_column(db, settings, drive):
    """An index written before Titlebars existed gets the column inserted in place and is rewritten as a whole."""
    from zoneinfo import ZoneInfo

    from mg_archive_bot.services import tracking
    from mg_archive_bot.services.sheets import InMemorySheetsClient, SheetsError

    tz = ZoneInfo("Asia/Singapore")
    col = tracking.HEADERS.index
    at = col("Titlebars")
    assert tracking.PREVIOUS_HEADERS == [h for h in tracking.HEADERS if h != "Titlebars"] and len(tracking.PREVIOUS_HEADERS) == 26
    table_range, wide_range = f"'Projects'!A:{tracking.LAST_COL}", "'Projects'!A:AB"
    insert = {"insertDimension": {"range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": at, "endIndex": at + 1}, "inheritFromBefore": True}}

    def legacy(rows: list[list[str]], notes: list[str] | None = None) -> InMemorySheetsClient:
        """The sheet as the previous version left it; *notes* is a column a teammate added right of the table."""
        client = InMemorySheetsClient()
        old = [list(tracking.PREVIOUS_HEADERS), *[[c for i, c in enumerate(r) if i != at] for r in rows]]
        if notes is not None:
            client.columns["legacy"] = {"Projects": 27}
            old = [row + [""] * (26 - len(row)) + [note] for row, note in zip(old, ["Notes", *notes], strict=True)]
        client.books["legacy"] = {"Projects": old}
        assert client.sheet_grids("legacy") == {"Projects": (0, 27 if notes is not None else 26)}
        return client

    def kinds(client) -> list[str]:
        return [next(iter(r)) for r in client.requests]

    def whole(client) -> list[list[str]]:
        """Every column the tab has at this moment (a range beyond the grid is refused)."""
        width = client.sheet_grids("legacy")["Projects"][1]
        return client.get_values("legacy", f"'Projects'!A:{tracking.column_letter(width)}")

    with session_scope() as s:
        p = project_service.create_draft(s, "Opening", 1, "Lead", 2026)
        for category in (AssetCategory.TITLEBARS, AssetCategory.PSD):
            project_service.set_declaration(s, p, category, True)
        project_service.set_metadata_field(s, p, "description", "Opening film")
        await project_service.provision_folders(s, p, drive, settings)
        other = project_service.create_draft(s, "Worship", 1, "Lead", 2026)
        await project_service.provision_folders(s, other, drive, settings)
        new_row, other_row = tracking.build_row(s, p, tz), tracking.build_row(s, other, tz)
        all_rows = tracking.all_rows(s, tz)
        assert all_rows == [new_row, other_row] and other_row[-1] == ""  # one project has no description
        expected = _as_read([tracking.HEADERS, new_row, other_row])
        assert [new_row[col(h)] for h in ("Titlebars", "PSD")] == ["Yes", "Yes"] and [other_row[col(h)] for h in ("Titlebars", "PSD")] == ["No", "No"]

        # 1. an update is the first thing to touch the sheet: nothing at all is changed until the rebuild
        sheets = legacy(all_rows)
        before = sheets.get_values("legacy", "'Projects'!A:Z")
        assert tracking.layout_of(before[0]) == "previous"
        assert tracking.upsert_row(sheets, "legacy", new_row) == "needs_rebuild"
        assert sheets.requests == [] and sheets.sheet_grids("legacy") == {"Projects": (0, 26)}
        assert sheets.get_values("legacy", "'Projects'!A:Z") == before
        assert tracking.rebuild(sheets, "legacy", all_rows) == 2
        assert sheets.get_values("legacy", table_range) == expected
        # the column was inserted where it belongs (one attempt, never repeated blindly); the grid grew with it,
        # and the filter, the widths and the tab name were left to the user
        assert sheets.requests == [insert] and sheets.single_attempt == [insert]
        assert sheets.sheet_grids("legacy") == {"Projects": (0, 27)}
        # from now on it is a plain update, and neither a sync nor a rebuild sends any request
        sheets.requests.clear()
        assert tracking.upsert_row(sheets, "legacy", new_row) == "updated"
        assert tracking.rebuild(sheets, "legacy", all_rows) == 2 and sheets.requests == []
        assert tracking.delete_row(sheets, "legacy", p.id) == "deleted" and tracking.delete_row(sheets, "legacy", 999) == "absent"
        assert sheets.get_values("legacy", table_range) == _as_read([tracking.HEADERS, other_row])

        # 2. a revoke is the first thing to touch the sheet: the row is not cut out of the old layout
        sheets = legacy(all_rows)
        assert tracking.delete_row(sheets, "legacy", p.id) == "needs_rebuild"
        assert sheets.requests == [] and sheets.get_values("legacy", "'Projects'!A:Z") == before

        # 3. a column that a teammate keeps next to the table moves with it instead of being overwritten
        sheets = legacy(all_rows, notes=["client asked for a recut", "waiting for the fonts licence"])
        assert tracking.layout_of(sheets.get_values("legacy", "'Projects'!A1:AA1")[0]) == "previous"
        assert tracking.rebuild(sheets, "legacy", all_rows) == 2
        assert sheets.requests == [insert] and sheets.sheet_grids("legacy") == {"Projects": (0, 28)}
        wide = sheets.get_values("legacy", wide_range)
        assert [row[:27] for row in wide] == [row + [""] * (27 - len(row)) for row in expected]
        assert [row[27] for row in wide] == ["Notes", "client asked for a recut", "waiting for the fonts licence"]
        assert tracking.rebuild(sheets, "legacy", all_rows) == 2  # the nightly rebuild leaves it alone as well
        assert sheets.get_values("legacy", wide_range) == wide and sheets.requests == [insert]

        # 4. whatever fails, and whether or not the failed request was applied, the next sync finishes the job:
        #    the column is inserted exactly once, the notes survive, the table is never empty or half written
        attempts = 0
        for lost_answer in (False, True):
            for k in range(1, 20):
                inner = legacy(all_rows, notes=["note one", "note two"])
                start = whole(inner)
                broken = _CountingSheets(inner, fail_at=k, lost_answer=lost_answer)
                try:
                    tracking.rebuild(broken, "legacy", all_rows)
                except SheetsError:
                    attempts += 1
                else:
                    assert len(broken.calls) < k  # the rebuild needs fewer calls than k: nothing failed
                left = whole(inner)
                states = {
                    "old": start,
                    "inserted": [row[:at] + [""] + row[at:] for row in start],
                    "new": [row[:27] + [""] * (27 - len(row[:27])) + [note] for row, note in zip(expected, ["Notes", "note one", "note two"], strict=True)],
                }
                assert left in states.values(), (k, lost_answer)
                outcome = tracking.upsert_row(inner, "legacy", new_row)
                assert outcome == ("updated" if left == states["new"] else "needs_rebuild"), (k, lost_answer)
                assert tracking.rebuild(inner, "legacy", all_rows) == 2
                assert inner.get_values("legacy", wide_range) == states["new"], (k, lost_answer)
                assert [r for r in inner.requests if "insertDimension" in r] == [insert], (k, lost_answer)
                assert "setBasicFilter" not in kinds(inner), (k, lost_answer)  # the user's filter was never reset
        assert attempts >= 8  # every request of the rebuild was made to fail in both ways

        # 5. the table is written by ONE request starting at A1; nothing is cleared first
        counting = _CountingSheets(legacy(all_rows))
        assert tracking.rebuild(counting, "legacy", all_rows) == 2
        writes = [args for name, args in counting.calls if name == "update_values"]
        assert len(writes) == 1 and writes[0][1] == f"'Projects'!A1:{tracking.LAST_COL}3" and writes[0][2][0] == tracking.HEADERS
        assert not [name for name, _ in counting.calls if name in ("clear_values", "append_values")]


@pytest.mark.asyncio
async def test_index_sheet_rebuild_is_safe_on_a_current_sheet(db, settings, drive):
    from zoneinfo import ZoneInfo

    from mg_archive_bot.services import tracking
    from mg_archive_bot.services.sheets import InMemorySheetsClient, SheetsError

    tz = ZoneInfo("Asia/Singapore")
    table_range = f"'Projects'!A:{tracking.LAST_COL}"
    with session_scope() as s:
        rows = []
        for name in ("One", "Two", "Three"):
            p = project_service.create_draft(s, name, 1, "Lead", 2026)
            await project_service.provision_folders(s, p, drive, settings)
            rows.append(tracking.build_row(s, p, tz))
        sheets = InMemorySheetsClient()
        sheet_id, _ = tracking.ensure_sheet(s, drive, sheets, settings)
        assert tracking.rebuild(sheets, sheet_id, rows) == 3
        full = _as_read([tracking.HEADERS, *rows])
        assert sheets.get_values(sheet_id, table_range) == full
        # somebody keeps notes to the right of the table
        sheets.batch_update(sheet_id, [{"appendDimension": {"sheetId": 0, "dimension": "COLUMNS", "length": 1}}])
        sheets.update_values(sheet_id, "'Projects'!AB1:AB4", [["Notes"], ["n1"], ["n2"], ["n3"]])
        sheets.requests.clear()

        # a rebuild that fails leaves the table as it was (it is never emptied first) ...
        for k in range(1, 6):
            for lost_answer in (False, True):
                broken = _CountingSheets(sheets, fail_at=k, lost_answer=lost_answer)
                try:
                    tracking.rebuild(broken, sheet_id, rows)
                except SheetsError:
                    pass
                assert sheets.get_values(sheet_id, table_range) == full, (k, lost_answer)
                # ... so the next sync is an ordinary one and the user's filter and widths are not reset
                assert tracking.upsert_row(sheets, sheet_id, rows[0]) == "updated", (k, lost_answer)
        assert sheets.requests == []

        # fewer projects than before: the left-over rows are blanked by the same single write
        assert tracking.rebuild(sheets, sheet_id, rows[:1]) == 1
        assert sheets.get_values(sheet_id, table_range) == full[:2]
        assert [r[27] for r in sheets.get_values(sheet_id, "'Projects'!A:AB")] == ["Notes", "n1", "n2", "n3"]  # not the bot's column
        assert tracking.rebuild(sheets, sheet_id, []) == 0 and sheets.get_values(sheet_id, table_range) == [tracking.HEADERS]
        assert sheets.requests == []

        # an unknown or empty header: the grid, header row and filter are set up again, then the widths are fitted
        for content in ([["garbage"]], []):
            own = InMemorySheetsClient()
            own.books["own"] = {"Sheet1": [list(r) for r in content]}
            assert tracking.upsert_row(own, "own", rows[0]) == "needs_rebuild"
            assert tracking.rebuild(own, "own", rows) == 3
            assert own.get_values("own", table_range) == full and own.sheet_grids("own") == {"Projects": (0, 27)}
            assert [next(iter(r)) for r in own.requests] == [
                "updateSheetProperties", "appendDimension", "updateSheetProperties", "repeatCell", "setBasicFilter", "autoResizeDimensions",
            ]
            assert own.requests[0]["updateSheetProperties"]["properties"] == {"sheetId": 0, "title": "Projects"}
            assert "title" not in own.requests[2]["updateSheetProperties"]["properties"]
            assert own.requests[4]["setBasicFilter"]["filter"]["range"]["endColumnIndex"] == 27
            assert own.single_attempt == []

        # only the cosmetic last step fails: the data is complete and the error is not raised
        own = InMemorySheetsClient()
        real_batch = own.batch_update

        def no_resize(spreadsheet_id, requests, **kwargs):
            if any("autoResizeDimensions" in r for r in requests):
                raise SheetsError("Google Sheets error: backend unavailable")
            real_batch(spreadsheet_id, requests, **kwargs)

        own.batch_update = no_resize
        assert tracking.rebuild(own, "own", rows) == 3
        assert own.get_values("own", table_range) == full and tracking.upsert_row(own, "own", rows[1]) == "updated"


def test_fake_sheet_behaves_like_google_where_it_matters():
    from mg_archive_bot.services.sheets import InMemorySheetsClient, SheetsError

    sheets = InMemorySheetsClient()
    sheets.books["book"] = {"Budget": [["a", "b"]], "Other": []}
    assert sheets.sheet_grids("book") == {"Budget": (0, 26), "Other": (1, 26)}
    # a batch is all or nothing, and a tab keeps its id when it is renamed
    with pytest.raises(SheetsError):
        sheets.batch_update("book", [
            {"appendDimension": {"sheetId": 0, "dimension": "COLUMNS", "length": 3}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": 0, "endColumnIndex": 40}}}},
        ])
    assert sheets.sheet_grids("book") == {"Budget": (0, 26), "Other": (1, 26)} and sheets.requests == []
    with pytest.raises(SheetsError):
        sheets.batch_update("book", [{"updateSheetProperties": {"properties": {"sheetId": 0, "title": "Other"}, "fields": "title"}}])
    with pytest.raises(SheetsError):
        sheets.batch_update("book", [{"appendDimension": {"sheetId": 7, "dimension": "COLUMNS", "length": 1}}])
    sheets.batch_update("book", [{"updateSheetProperties": {"properties": {"sheetId": 0, "title": "Projects"}, "fields": "title"}}])
    sheets.batch_update("book", [{"appendDimension": {"sheetId": 0, "dimension": "COLUMNS", "length": 2}}])
    assert sheets.sheet_grids("book") == {"Other": (1, 26), "Projects": (0, 28)}
    # ranges are honoured cell by cell; reads drop empty cells and rows at the end
    sheets.update_values("book", "'Projects'!B2:C3", [["x", ""], ["", ""]])
    assert sheets.get_values("book", "'Projects'!A:AB") == [["a", "b"], ["", "x"]]
    assert sheets.get_values("book", "'Projects'!B1:B2") == [["b"], ["x"]] and sheets.get_values("book", "'Projects'!C:C") == []
    sheets.update_values("book", "'Projects'!AB1:AB1", [["note"]])
    sheets.clear_values("book", "'Projects'!A1:B1")
    assert sheets.get_values("book", "'Projects'!A:AB") == [[""] * 27 + ["note"], ["", "x"]]
    sheets.append_values("book", "'Projects'!A:AA", [["1", "new"]])  # below the table found in A..AA, not below AB
    assert sheets.get_values("book", "'Projects'!A:B") == [[], ["", "x"], ["1", "new"]]
    for call in (
        lambda: sheets.update_values("book", "'Projects'!A1:B1", [["1", "2", "3"]]),
        lambda: sheets.update_values("book", "'Projects'!A1:B1", [["1"], ["2"]]),
        lambda: sheets.get_values("book", "'Projects'!A1:AC1"),
        lambda: sheets.get_values("book", "'Projects'!1:1 oops"),
    ):
        with pytest.raises(SheetsError):
            call()
    # inserting a column moves everything right of it, including what lies beyond the table
    sheets.batch_update("book", [{"insertDimension": {"range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "inheritFromBefore": True}}], retry=False)
    assert sheets.sheet_grids("book")["Projects"] == (0, 29)
    assert sheets.get_values("book", "'Projects'!A:AC") == [[""] * 28 + ["note"], ["", "", "x"], ["1", "", "new"]]
    assert sheets.single_attempt == [sheets.requests[-1]]


def test_titlebars_folder_name_can_be_overridden(settings):
    from mg_archive_bot.config import Settings

    assert settings.folder_names()["titlebars"] == ""  # empty means "use the default"
    assert {f.key: f.name for f in build_folder_tree(settings.folder_names())}["titlebars"] == "Titlebars"
    custom = Settings(telegram_bot_token="123:TEST", super_admin_telegram_id=SUPER_ADMIN_ID, google_auth_mode="fake",
                      folder_name_titlebars="Title Bars", _env_file=None)
    names = {f.key: f.name for f in build_folder_tree(custom.folder_names())}
    assert names["titlebars"] == "Title Bars" and names["timeline"] == "Timeline" and "titlebars_prores" not in names


@pytest.mark.asyncio
async def test_ensure_folders_keeps_what_drive_created_before_failing(db, settings, drive):
    from mg_archive_bot.services.drive import DriveError

    with session_scope() as s:
        p = project_service.create_draft(s, "Titles", 1, "Lead", 2026)
        await project_service.provision_folders(s, p, drive, settings)
        s.commit()
        pid = p.id
        for category in (AssetCategory.PSD, AssetCategory.TITLEBARS):  # two folders are missing now
            project_service.set_declaration(s, p, category, True)
        s.commit()
        real = drive.create_folder

        def flaky(name, parent_id):
            if name == "Titlebars":
                raise DriveError("Google Drive is unavailable")
            return real(name, parent_id)

        drive.create_folder = flaky
        with pytest.raises(DriveError):
            await project_service.ensure_folders(s, p, drive, settings)
        s.rollback()  # what every caller does when it sees the error
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.folder("psd") is not None and p.folder("titlebars") is None
        assert drive.path_of(p.folder("psd").drive_id).endswith("Working File/PSD")
        drive.create_folder = real
        assert await project_service.ensure_folders(s, p, drive, settings) == ["titlebars"]  # only the rest
        assert [f.name for f in drive.list_children(p.folder("final_render").drive_id)].count("Titlebars") == 1
        assert [f.name for f in drive.list_children(p.folder("working_file").drive_id)].count("PSD") == 1


def test_fake_drive_moves_folders_like_google(drive):
    from mg_archive_bot.services.drive import DriveError

    a = drive.create_folder("A", drive.ROOT_ID)
    b = drive.create_folder("B", drive.ROOT_ID)
    project = drive.create_folder("Project", a.id)
    inner = drive.create_folder("Working File", project.id)
    clip = drive.put_file(inner.id, "clip.mov")
    moved = drive.move(project.id, b.id)
    assert moved.id == project.id and moved.parents == (b.id,) and moved.name == "Project"
    assert drive.path_of(clip.id) == "Archive Root/B/Project/Working File/clip.mov"  # everything inside travels along
    assert drive.list_children(a.id) == [] and [f.id for f in drive.list_children(b.id)] == [project.id]
    assert drive.move(project.id, drive.ROOT_ID, "Project (2)").name == "Project (2)"
    assert drive.path_of(inner.id) == "Archive Root/Project (2)/Working File"
    for file_id, parent in ((project.id, inner.id), (project.id, project.id), ("missing", a.id), (project.id, "missing"), (project.id, clip.id)):
        with pytest.raises(DriveError):
            drive.move(file_id, parent)
    drive.delete(b.id)
    with pytest.raises(DriveError):
        drive.move(project.id, b.id)  # a folder in the trash is not a destination
    assert drive.path_of(project.id) == "Archive Root/Project (2)"


def test_google_drive_move_is_one_request():
    """The real client moves (and renames) with a single files.update, for Shared Drives too."""
    from mg_archive_bot.services.drive import DriveError, GoogleDriveClient

    class Request:
        def __init__(self, result):
            self.result = result

        def execute(self, num_retries=0):
            return self.result

    class Files:
        def __init__(self):
            self.item = {"id": "f1", "name": "Opening", "mimeType": "application/vnd.google-apps.folder", "parents": ["old"]}
            self.updates: list[dict] = []

        def get(self, **kwargs):
            return Request(dict(self.item))

        def update(self, **kwargs):
            self.updates.append(kwargs)
            if "addParents" in kwargs:
                self.item["parents"] = [kwargs["addParents"]]
            self.item.update(kwargs.get("body") or {})
            return Request(dict(self.item))

    files = Files()
    client = GoogleDriveClient(credentials=None)
    client._service = lambda: type("Service", (), {"files": lambda self: files})()  # type: ignore[method-assign]
    moved = client.move("f1", "new")
    assert moved.parents == ("new",) and moved.name == "Opening"
    assert files.updates == [{"fileId": "f1", "body": {}, "fields": GoogleDriveClient.FIELDS, "supportsAllDrives": True, "addParents": "new", "removeParents": "old"}]
    assert client.move("f1", "newer", "Opening (2)").name == "Opening (2)"
    assert files.updates[-1]["body"] == {"name": "Opening (2)"} and files.updates[-1]["removeParents"] == "new"
    # already there with that name: nothing is sent
    assert client.move("f1", "newer", "Opening (2)").parents == ("newer",) and len(files.updates) == 2
    # only the name differs: a rename in place, no parent is added or removed
    client.move("f1", "newer", "Opening")
    assert files.updates[-1] == {"fileId": "f1", "body": {"name": "Opening"}, "fields": GoogleDriveClient.FIELDS, "supportsAllDrives": True}

    # Google applied the move but the answer was lost, and the repeated request is refused: that is a success
    class Lost(Request):
        def __init__(self, files, apply):
            self.files, self.apply = files, apply

        def execute(self, num_retries=0):
            if self.apply:
                self.files.item["parents"] = ["lost-and-found"]
                self.files.item["name"] = "Opening (3)"
            raise OSError("connection reset")

    real_update = files.update
    files.update = lambda **kwargs: Lost(files, apply=True)  # type: ignore[method-assign]
    moved = client.move("f1", "lost-and-found", "Opening (3)")
    assert moved.parents == ("lost-and-found",) and moved.name == "Opening (3)"
    # it really was not applied: the failure is reported
    files.update = lambda **kwargs: Lost(files, apply=False)  # type: ignore[method-assign]
    with pytest.raises(DriveError, match="unreachable"):
        client.move("f1", "elsewhere")
    assert files.item["parents"] == ["lost-and-found"]
    files.update = real_update  # type: ignore[method-assign]

    # the folder is gone: nothing is sent
    sent = len(files.updates)
    files.get = lambda **kwargs: Request(None)  # type: ignore[method-assign]
    client.get_file = lambda file_id: None  # type: ignore[method-assign]
    with pytest.raises(DriveError, match="no longer exists"):
        client.move("f1", "x")
    assert len(files.updates) == sent


@pytest.mark.asyncio
async def test_move_project_service(db, settings, drive):
    from mg_archive_bot.services import collections as collection_service

    with session_scope() as s:
        bf = collection_service.create_collection(s, "Building Fund 2026", 1)
        easter = collection_service.create_collection(s, "Easter", 1)
        p = project_service.create_draft(s, "God I'm Just Thankful", 1, "Lead", 2026, bf)
        with pytest.raises(ProjectError, match="Drafts and cancelled"):
            await project_service.move_project(s, p, None, drive, settings)
        await project_service.provision_folders(s, p, drive, settings)
        s.commit()
        ids = {f.key: f.drive_id for f in p.folders}
        assert drive.path_of(ids["ae"]) == "Archive Root/Building Fund 2026/God I'm Just Thankful/Working File/AE"
        assert p.full_name == "Building Fund 2026 / God I'm Just Thankful" and p.collection == "Building Fund 2026"
        assert easter.drive_id is None  # its folder does not exist yet

        with pytest.raises(ProjectError, match="already in “Building Fund 2026”"):
            await project_service.move_project(s, p, bf, drive, settings)
        # out of the collection
        assert await project_service.move_project(s, p, None, drive, settings) == "Building Fund 2026 / God I'm Just Thankful"
        s.commit()
        assert (p.collection_id, p.collection, p.collection_folder, p.full_name) == (None, "", None, "God I'm Just Thankful")
        assert drive.path_of(ids["ae"]) == "Archive Root/God I'm Just Thankful/Working File/AE"
        assert {f.key: f.drive_id for f in p.folders} == ids and p.drive_root_id == ids["root"]  # same folders, same links
        assert [f.name for f in drive.list_children(bf.drive_id)] == []  # the collection stays, now empty
        with pytest.raises(ProjectError, match="not in a collection"):
            await project_service.move_project(s, p, None, drive, settings)
        # into a collection whose folder is created on the way
        assert await project_service.move_project(s, p, easter, drive, settings) == "God I'm Just Thankful"
        s.commit()
        assert easter.drive_id is not None and drive.path_of(easter.drive_id) == "Archive Root/Easter"
        assert (p.collection_id, p.collection, p.full_name) == (easter.id, "Easter", "Easter / God I'm Just Thankful")
        assert drive.path_of(ids["ae"]) == "Archive Root/Easter/God I'm Just Thankful/Working File/AE"
        # straight from one collection to another
        await project_service.move_project(s, p, bf, drive, settings)
        s.commit()
        assert drive.path_of(ids["root"]) == "Archive Root/Building Fund 2026/God I'm Just Thankful" and p.collection == "Building Fund 2026"

        # names stay unique inside the destination
        twin = project_service.create_draft(s, "god i'm just thankful", 1, "Lead", 2026)
        await project_service.provision_folders(s, twin, drive, settings)
        s.commit()
        with pytest.raises(ProjectError, match="already exists in “Building Fund 2026”. Rename one of them first"):
            await project_service.move_project(s, twin, bf, drive, settings)
        with pytest.raises(ProjectError, match="already exists at the top level"):
            await project_service.move_project(s, p, None, drive, settings)
        assert drive.path_of(twin.drive_root_id) == "Archive Root/god i'm just thankful" and twin.collection_id is None
        # a folder of that name that the bot does not know: the Drive folder gets a suffix, the project keeps its name
        drive.create_folder("God I'm Just Thankful", easter.drive_id)
        await project_service.move_project(s, p, easter, drive, settings)
        s.commit()
        assert p.name == "God I'm Just Thankful" and p.folder("root").name == "God I'm Just Thankful (2)"
        assert drive.path_of(ids["root"]) == "Archive Root/Easter/God I'm Just Thankful (2)"
        # ... and gets its own name back where it is free
        await project_service.move_project(s, p, bf, drive, settings)
        s.commit()
        assert p.folder("root").name == "God I'm Just Thankful" and drive.path_of(ids["root"]) == "Archive Root/Building Fund 2026/God I'm Just Thankful"

        # archived projects can be re-filed, cancelled ones cannot
        p.status = ProjectStatus.ARCHIVED
        await project_service.move_project(s, p, easter, drive, settings)
        s.commit()
        assert drive.path_of(ids["root"]) == "Archive Root/Easter/God I'm Just Thankful (2)" and p.status == ProjectStatus.ARCHIVED
        p.status = ProjectStatus.CANCELLED
        with pytest.raises(ProjectError, match="Drafts and cancelled"):
            await project_service.move_project(s, p, bf, drive, settings)
        assert p.collection_id == easter.id and drive.path_of(ids["root"]) == "Archive Root/Easter/God I'm Just Thankful (2)"


@pytest.mark.asyncio
async def test_move_project_inside_a_configured_archive_folder(db, settings, drive):
    """With DRIVE_ROOT_FOLDER_ID set, "top level" is that folder, never the top of the Drive."""
    from mg_archive_bot.services import collections as collection_service

    archive = drive.create_folder("Shared Archive", drive.ROOT_ID)
    configured = settings.model_copy(update={"drive_root_folder_id": archive.id})
    with session_scope() as s:
        easter = collection_service.create_collection(s, "Easter", 1)
        p = project_service.create_draft(s, "Song", 1, "Lead", 2026, easter)
        await project_service.provision_folders(s, p, drive, configured)
        s.commit()
        root_id = p.drive_root_id
        assert drive.path_of(root_id) == "Archive Root/Shared Archive/Easter/Song"
        await project_service.move_project(s, p, None, drive, configured)
        s.commit()
        assert drive.path_of(root_id) == "Archive Root/Shared Archive/Song"
        # somebody moved the folder by hand in Google Drive: the bot's move to that same place repairs the record
        drive.move(root_id, easter.drive_id)
        assert p.collection_id is None
        await project_service.move_project(s, p, easter, drive, configured)
        s.commit()
        assert (p.collection_id, p.folder("root").name) == (easter.id, "Song")
        assert drive.path_of(root_id) == "Archive Root/Shared Archive/Easter/Song"
        assert [f.name for f in drive.list_children(easter.drive_id)] == ["Song"]


@pytest.mark.asyncio
async def test_collection_folder_is_checked_before_it_is_used(db, settings, drive):
    from mg_archive_bot.services import collections as collection_service
    from mg_archive_bot.services.collections import ensure_collection_folder

    with session_scope() as s:
        # a folder somebody made by hand is re-used; a project's own folder never is
        by_hand = drive.create_folder("Hand Made", drive.ROOT_ID)
        hand = collection_service.create_collection(s, "Hand Made", 1)
        assert await ensure_collection_folder(s, hand, drive, settings) is False and hand.drive_id == by_hand.id  # found, not made
        project = project_service.create_draft(s, "Christmas", 1, "Lead", 2026)
        await project_service.provision_folders(s, project, drive, settings)
        s.commit()
        christmas = collection_service.create_collection(s, "Christmas", 1)
        with pytest.raises(ProjectError, match="A project at the top level is already called “Christmas”"):
            await ensure_collection_folder(s, christmas, drive, settings)
        assert christmas.drive_id is None
        with pytest.raises(ProjectError, match="already called “Christmas”"):
            await ensure_collection_folder(s, christmas, drive, settings, moving_root_id="some-other-folder")
        assert await ensure_collection_folder(s, christmas, drive, settings, moving_root_id=project.drive_root_id) is True
        made = christmas.drive_id
        assert made not in (None, project.drive_root_id) and drive.path_of(made) == "Archive Root/Christmas"
        s.commit()
        # a folder that is fine is kept, and asked about exactly once
        calls = []
        real_get = drive.get_file
        drive.get_file = lambda file_id: calls.append(file_id) or real_get(file_id)
        assert await ensure_collection_folder(s, christmas, drive, settings) is False and christmas.drive_id == made
        assert calls == [made]
        drive.get_file = real_get
        await project_service.move_project(s, project, christmas, drive, settings)
        s.commit()
        assert drive.path_of(project.drive_root_id) == "Archive Root/Christmas/Christmas"
        # the name rule that the wizard and the move apply before anything is created
        top = project_service.create_draft(s, "Standalone", 1, "Lead", 2026)
        assert collection_service.taken_by_top_level_project(s, "standalone") is False  # a draft has no folder yet
        await project_service.provision_folders(s, top, drive, settings)
        s.commit()
        assert collection_service.taken_by_top_level_project(s, " STANDALONE ") is True
        assert collection_service.taken_by_top_level_project(s, "Standalone", except_project_id=top.id) is False
        assert collection_service.taken_by_top_level_project(s, "Christmas") is False  # that project is inside a collection now


@pytest.mark.asyncio
async def test_a_new_collection_is_stored_only_after_the_move(db, settings, drive):
    from mg_archive_bot.models import Collection
    from mg_archive_bot.services import collections as collection_service
    from mg_archive_bot.services.drive import DriveError

    with session_scope() as s:
        p = project_service.create_draft(s, "Song", 1, "Lead", 2026)
        await project_service.provision_folders(s, p, drive, settings)
        other = project_service.create_draft(s, "Taken", 1, "Lead", 2026)
        await project_service.provision_folders(s, other, drive, settings)
        s.commit()
        root_id = p.drive_root_id

        def folders_at_root() -> list[str]:
            return sorted(f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder)

        # refused by name: nothing is created anywhere
        with pytest.raises(ProjectError, match="already called “Taken”"):
            await project_service.move_project(s, p, Collection(name="Taken", created_by=1), drive, settings)
        s.rollback()
        assert collection_service.list_collections(s) == [] and folders_at_root() == ["Song", "Taken"]

        # Google Drive fails: the folder that was made for this move is removed again, no collection is stored
        real_move = drive.move
        seen_during_move: list[list[str]] = []

        def failing(*args, **kwargs):
            with session_scope() as other_session:  # what everybody else sees while the move is in flight
                seen_during_move.append([c.name for c in collection_service.list_collections(other_session)])
            raise DriveError("Google Drive error 500")

        drive.move = failing
        with pytest.raises(DriveError):
            await project_service.move_project(s, p, Collection(name="Easter", created_by=1), drive, settings)
        s.rollback()
        assert seen_during_move == [[]] and collection_service.list_collections(s) == []
        assert folders_at_root() == ["Song", "Taken"] and drive.path_of(root_id) == "Archive Root/Song"
        # ... but a folder somebody made by hand is only borrowed, never thrown away
        by_hand = drive.create_folder("Easter", drive.ROOT_ID)
        with pytest.raises(DriveError):
            await project_service.move_project(s, p, Collection(name="Easter", created_by=1), drive, settings)
        s.rollback()
        assert drive.get_file(by_hand.id).trashed is False and folders_at_root() == ["Easter", "Song", "Taken"]
        drive.move = real_move

        # success: stored now, with the folder it was given
        await project_service.move_project(s, p, Collection(name="Easter", created_by=1), drive, settings)
        s.commit()
        stored = collection_service.find_by_name(s, "easter")
        assert stored is not None and stored.id is not None and stored.drive_id == by_hand.id and stored.created_by == 1
        assert (p.collection_id, p.collection, p.full_name) == (stored.id, "Easter", "Easter / Song")
        assert drive.path_of(root_id) == "Archive Root/Easter/Song"

        # a wizard stored the same name while the move was in flight: that record is used, not a second one
        def racing(*args, **kwargs):
            with session_scope() as other_session:
                collection_service.create_collection(other_session, "Pentecost", 2)
            return real_move(*args, **kwargs)

        drive.move = racing
        await project_service.move_project(s, other, Collection(name="pentecost", created_by=1), drive, settings)
        s.commit()
        drive.move = real_move
        names = [c.name for c in collection_service.list_collections(s)]
        assert names == ["Easter", "Pentecost"]
        pentecost = collection_service.find_by_name(s, "Pentecost")
        assert other.collection_id == pentecost.id and pentecost.created_by == 2 and pentecost.drive_id is not None
        assert drive.path_of(other.drive_root_id) == "Archive Root/pentecost/Taken"


@pytest.mark.asyncio
async def test_collections_recorded_by_earlier_versions_keep_working(db, settings, drive):
    """Earlier versions re-used a top-level project's folder for a collection of the same name."""
    from mg_archive_bot.services import collections as collection_service
    from mg_archive_bot.services.collections import ensure_collection_folder

    with session_scope() as s:
        owner = project_service.create_draft(s, "BF", 1, "Lead", 2026)
        await project_service.provision_folders(s, owner, drive, settings)
        # the state an earlier version left behind: the collection "BF" recorded the folder of the project "BF"
        # when its first sub-project was created, and that sub-project lives inside it
        bf = collection_service.create_collection(s, "BF", 1)
        first = project_service.create_draft(s, "Teaser", 1, "Lead", 2026)
        await project_service.provision_folders(s, first, drive, settings)
        drive.move(first.drive_root_id, owner.drive_root_id)
        bf.drive_id = owner.drive_root_id
        first.collection_folder, first.collection = bf, "BF"
        s.commit()
        assert drive.path_of(first.drive_root_id) == "Archive Root/BF/Teaser" and first.full_name == "BF / Teaser"

        member = project_service.create_draft(s, "Opening", 1, "Lead", 2026, bf)
        await project_service.provision_folders(s, member, drive, settings)  # the wizard still works
        s.commit()
        assert drive.path_of(member.drive_root_id) == "Archive Root/BF/Opening" and bf.drive_id == owner.drive_root_id
        song = project_service.create_draft(s, "Song", 1, "Lead", 2026)
        await project_service.provision_folders(s, song, drive, settings)
        s.commit()
        await project_service.move_project(s, song, bf, drive, settings)  # and so does a move into it
        s.commit()
        assert drive.path_of(song.drive_root_id) == "Archive Root/BF/Song" and bf.drive_id == owner.drive_root_id
        # the project that owns the folder cannot go into itself, and is told why
        with pytest.raises(ProjectError, match="uses this project's own Google Drive folder, and 3 project"):
            await project_service.move_project(s, owner, bf, drive, settings)
        s.rollback()
        # once the collection is empty it gets a folder of its own
        for p in (first, member, song):
            await project_service.move_project(s, p, None, drive, settings)
            s.commit()
        with pytest.raises(ProjectError, match="already called “BF”"):
            await ensure_collection_folder(s, bf, drive, settings)  # the owner still has the name
        s.rollback()
        await project_service.rename_project(s, owner, "BF Main", drive, settings)
        s.commit()
        assert await ensure_collection_folder(s, bf, drive, settings) is True
        s.commit()
        assert bf.drive_id != owner.drive_root_id and drive.path_of(bf.drive_id) == "Archive Root/BF"
        assert drive.path_of(owner.drive_root_id) == "Archive Root/BF Main"


@pytest.mark.asyncio
async def test_collection_with_only_cancelled_projects_can_be_used_again(db, settings, drive):
    from mg_archive_bot.services import collections as collection_service

    with session_scope() as s:
        bf = collection_service.create_collection(s, "BF 2025", 1)
        teaser = project_service.create_draft(s, "Teaser", 1, "Lead", 2026, bf)
        await project_service.provision_folders(s, teaser, drive, settings)
        song = project_service.create_draft(s, "Song", 1, "Lead", 2026)
        await project_service.provision_folders(s, song, drive, settings)
        s.commit()
        old_folder = bf.drive_id
        await project_service.revoke_project(s, teaser, drive, 1)
        s.commit()
        # the collection folder went to the trash too; the cancelled project could still be restored: refused
        drive.delete(old_folder)
        with pytest.raises(ProjectError, match="is in the trash, and 1 project"):
            await project_service.move_project(s, song, bf, drive, settings)
        s.rollback()
        # the trash was emptied: nothing of the cancelled project is left, the collection starts again
        for gone in [n for n in list(drive._nodes) if n == old_folder or drive.path_of(n).startswith("Archive Root/BF 2025/")]:
            del drive._nodes[gone]
        await project_service.move_project(s, song, bf, drive, settings)
        s.commit()
        assert bf.drive_id != old_folder and drive.path_of(song.drive_root_id) == "Archive Root/BF 2025/Song"
        # an active project whose folder is missing still blocks the replacement
        del drive._nodes[bf.drive_id]
        other = project_service.create_draft(s, "Other", 1, "Lead", 2026)
        await project_service.provision_folders(s, other, drive, settings)
        s.commit()
        with pytest.raises(ProjectError, match="cannot be found .* and 1 project"):
            await project_service.move_project(s, other, bf, drive, settings)


@pytest.mark.asyncio
async def test_names_are_compared_without_case_in_every_alphabet(db, settings, drive):
    from sqlalchemy import text

    from mg_archive_bot.models import Collection
    from mg_archive_bot.services import collections as collection_service

    with session_scope() as s:
        assert s.execute(text("select lower('ÄRZTE NOËL Ωmega ABC 复活节'), lower(NULL), lower(42)")).one() == ("ärzte noël ωmega abc 复活节", None, 42)
        stored = collection_service.create_collection(s, "Ärzte", 1)
        s.commit()
        for spelling in ("Ärzte", "ärzte", "ÄRZTE", "  ärzte "):
            assert collection_service.find_by_name(s, spelling) is stored, spelling
        assert collection_service.create_collection(s, "ÄRZTE", 1) is stored  # re-used, not doubled
        p = project_service.create_draft(s, "Élan", 1, "Lead", 2026)
        await project_service.provision_folders(s, p, drive, settings)
        s.commit()
        assert project_service.name_in_use(s, "ÉLAN") and project_service.name_in_use(s, "élan")
        assert collection_service.taken_by_top_level_project(s, "élan") is True
        with pytest.raises(ProjectError, match="already exists"):
            project_service.create_draft(s, "ÉLAN", 2, "Other", 2026)
        # typing an existing name for a move, in any spelling, leads to that collection
        song = project_service.create_draft(s, "Song", 1, "Lead", 2026)
        await project_service.provision_folders(s, song, drive, settings)
        s.commit()
        for spelling in ("Ärzte", "ÄRZTE"):
            target = collection_service.find_by_name(s, spelling) or Collection(name=spelling, created_by=1)
            assert target is stored
        await project_service.move_project(s, song, stored, drive, settings)
        s.commit()
        assert [c.name for c in collection_service.list_collections(s)] == ["Ärzte"]
        assert drive.path_of(song.drive_root_id) == "Archive Root/Ärzte/Song"
