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
        user_service.set_access_password(s, "new-password-9")
        assert user_service.verify_access_password(s, "new-password-9")
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
        "INSERT INTO projects (id, name, status, created_by, last_complete) VALUES (1, 'Old', 'ACTIVE', 1, 0);"
    )
    conn.commit()
    conn.close()
    engine = init_db(f"sqlite:///{path}")
    with session_scope() as s:
        p = project_service.get_project(s, 1)
        assert p.name == "Old" and p.collection_id is None and p.full_name == "Old"
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
        assert sheets.get_values(sheet_id, "'Projects'!A1:Y1")[0] == tracking.HEADERS
        assert any("frozenRowCount" in str(r) for r in sheets.requests) and any("setBasicFilter" in r for r in sheets.requests)
        p = project_service.create_draft(s, "Opening", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.TIMELINE, True)
        project_service.set_metadata_field(s, p, "tags", "worship, gold")
        project_service.toggle_assignment(s, p, 11, AssetCategory.ALL)
        await project_service.provision_folders(s, p, drive, settings)
        row = tracking.build_row(s, p, tz)
        assert row[:5] == [str(p.id), "", "Opening", "Active", "2026"] and row[9] == "gold, worship" and row[11] == "Yes" and row[12] == "No"
        assert row[15] == "Alice" and row[23] == p.drive_link
        assert tracking.upsert_row(sheets, sheet_id, row) == "appended"
        p.status = ProjectStatus.ARCHIVED
        p.verified_by, p.verified_at = 1, p.created_at
        s.flush()
        assert tracking.upsert_row(sheets, sheet_id, tracking.build_row(s, p, tz)) == "updated"
        values = sheets.get_values(sheet_id, "'Projects'!A:Y")
        assert len(values) == 2 and values[1][3] == "Archived" and values[1][18] != ""
        # rebuild rewrites everything from the database (drafts excluded)
        project_service.create_draft(s, "Draft only", 1, "Lead", 2026)
        assert tracking.rebuild(sheets, sheet_id, tracking.all_rows(s, tz)) == 1
        assert [r[2] for r in sheets.get_values(sheet_id, "'Projects'!A:Y")[1:]] == ["Opening"]


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
    assert [f.key for f in tree if f.is_leaf] == ["fonts", "ae", "psd", "timeline_prores", "timeline_hap", "contin_prores", "contin_hap", "lyrics_png"]
