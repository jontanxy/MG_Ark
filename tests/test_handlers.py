"""End-to-end handler tests driven through the fake Telegram harness and the in-memory Drive."""
from __future__ import annotations

import pytest

from mg_archive_bot.constants import AssetCategory, PreviewStatus, ProjectStatus, Role
from mg_archive_bot.db import session_scope
from mg_archive_bot.models import PreviewAsset
from mg_archive_bot.services import groups as group_service
from mg_archive_bot.services import projects as project_service
from mg_archive_bot.services import users as user_service
from tests.conftest import PASSWORD, SUPER_ADMIN_ID
from tests.fakes import BotHarness, FakeChat, FakeUser

ADMIN = FakeUser(SUPER_ADMIN_ID, "Boss", username="boss")
LEAD = FakeUser(2001, "Lee", "Lead", username="lead")
DESIGNER = FakeUser(3001, "Dee", "Signer", username="dee")
STRANGER = FakeUser(4001, "Ran", "Dom")
GROUP = FakeChat(-100500, "supergroup", "MG Team")


@pytest.fixture
def harness(db, settings, drive):
    h = BotHarness(settings, drive)
    with session_scope() as s:
        user_service.ensure_super_admin(s, ADMIN.id, ADMIN.full_name, ADMIN.username)
        user_service.register_designer(s, LEAD.id, LEAD.full_name, LEAD.username)
        user_service.set_role(s, LEAD.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        user_service.register_designer(s, DESIGNER.id, DESIGNER.full_name, DESIGNER.username)
    return h


@pytest.fixture
def authorised_group(harness):
    with session_scope() as s:
        token = group_service.create_token(s, LEAD.id, 24)
        group_service.authorise_group(s, GROUP.id, GROUP.title, token)
    return GROUP


# ----------------------------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_password_registration(harness):
    bot = harness.bot
    await harness.command(STRANGER, "/start")
    assert "Enter access password" in bot.last(STRANGER.id)["text"]
    update = await harness.text(STRANGER, "wrong-pass")
    assert update.message.deleted  # password messages are removed from the chat
    assert "Incorrect password" in bot.last(STRANGER.id)["text"] and "4 attempts" in bot.last(STRANGER.id)["text"]
    await harness.text(STRANGER, PASSWORD)
    assert "User registered" in bot.last(STRANGER.id)["text"] and "Role = Designer" in bot.last(STRANGER.id)["text"]
    with session_scope() as s:
        assert user_service.get_user(s, STRANGER.id).role == Role.DESIGNER
    # second /start needs no password
    await harness.command(STRANGER, "/start")
    assert "Welcome back" in bot.last(STRANGER.id)["text"]
    # text without a pending prompt is treated as a search
    await harness.text(STRANGER, "gold")
    assert "No projects match" in bot.last(STRANGER.id)["text"]


@pytest.mark.asyncio
async def test_lockout_after_five_failures(harness):
    bot = harness.bot
    await harness.command(STRANGER, "/start")
    for _ in range(5):
        await harness.text(STRANGER, "nope")
    assert "Locked for 15 minutes" in bot.last(STRANGER.id)["text"]
    harness.bot_data.pop("limiters", None)  # unknown senders are answered once per minute; skip the wait
    await harness.command(STRANGER, "/start")
    assert "Too many failed attempts" in bot.last(STRANGER.id)["text"]
    harness.bot_data.pop("limiters", None)
    await harness.text(STRANGER, PASSWORD)  # no prompt pending → not accepted as a password
    assert "Send /start" in bot.last(STRANGER.id)["text"]


@pytest.mark.asyncio
async def test_revoked_user_cannot_reregister_or_act(harness):
    bot = harness.bot
    with session_scope() as s:
        user_service.revoke_user(s, DESIGNER.id, SUPER_ADMIN_ID)
    await harness.command(DESIGNER, "/start")
    assert "revoked" in bot.last(DESIGNER.id)["text"]
    await harness.command(DESIGNER, "/search gold")
    assert "revoked" in bot.last(DESIGNER.id)["text"]
    from mg_archive_bot.bot.access import set_prompt

    set_prompt(harness.ctx(DESIGNER), "password")
    await harness.text(DESIGNER, PASSWORD)
    assert "cannot re-register" in bot.last(DESIGNER.id)["text"]
    with session_scope() as s:
        assert user_service.get_user(s, DESIGNER.id).status.value == "REVOKED"


@pytest.mark.asyncio
async def test_super_admin_is_pinned_and_auto_registered(db, settings, drive):
    harness = BotHarness(settings, drive)
    await harness.command(ADMIN, "/start")
    assert "Super Admin" in harness.bot.last(ADMIN.id)["text"]
    await harness.command(ADMIN, "/whoami")
    assert "Super Admin" in harness.bot.last(ADMIN.id)["text"]


# ----------------------------------------------------------------------------------------
# Role & scope enforcement
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_designer_cannot_use_team_lead_or_admin_commands(harness):
    bot = harness.bot
    for cmd in ("/newproject", "/projects", "/creategroup"):
        await harness.command(DESIGNER, cmd)
        assert "requires the Team Lead role" in bot.last(DESIGNER.id)["text"]
    for cmd in ("/users", "/groups", "/setpassword"):
        await harness.command(DESIGNER, cmd)
        assert "requires the Super Admin role" in bot.last(DESIGNER.id)["text"]
    await harness.command(LEAD, "/users")
    assert "requires the Super Admin role" in bot.last(LEAD.id)["text"]
    # designers cannot press Team Lead buttons either
    q = await harness.press(DESIGNER, "pj:1:verify2")
    assert q.answers[-1][0].startswith("This action requires the Team Lead")
    q = await harness.press(DESIGNER, "ad:users")
    assert "Super Admin" in q.answers[-1][0]


@pytest.mark.asyncio
async def test_private_only_commands_are_refused_in_groups(harness, authorised_group):
    bot = harness.bot
    for cmd in ("/search gold", "/newproject", "/projects", "/users", "/creategroup"):
        await harness.command(LEAD, cmd, chat=authorised_group)
        assert "private chat" in bot.last(authorised_group.id)["text"]
    await harness.command(LEAD, "/status")  # group-only in private
    assert "only works inside an MG Group" in bot.last(LEAD.id)["text"]


@pytest.mark.asyncio
async def test_group_security_requires_user_and_group(harness, authorised_group):
    bot = harness.bot
    other = FakeChat(-100999, "supergroup", "Random chat")
    await harness.command(LEAD, "/status", chat=other)  # authorised user, unauthorised group → deny
    assert "not an authorised MG Group" in bot.last(other.id)["text"]
    await harness.command(STRANGER, "/status", chat=authorised_group)  # unauthorised user, authorised group → deny
    assert "not an authorised user" in bot.last(authorised_group.id)["text"]
    await harness.command(DESIGNER, "/status", chat=authorised_group)  # both authorised → allowed
    assert "No open archives" in bot.last(authorised_group.id)["text"]
    await harness.command(DESIGNER, "/remind", chat=authorised_group)  # designer cannot remind
    assert "requires the Team Lead role" in bot.last(authorised_group.id)["text"]


# ----------------------------------------------------------------------------------------
# MG Group provisioning
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creategroup_activate_flow(harness):
    bot = harness.bot
    await harness.command(LEAD, "/creategroup")
    text = bot.last(LEAD.id)["text"]
    token = text.split("<code>")[1].split("</code>")[0]
    assert token.startswith("MG-")
    # bot added by someone without a token → hint, chat noted as unauthorised
    await harness.chat_member(GROUP, DESIGNER, "left", "member")
    assert "not yet an authorised MG Group" in bot.last(GROUP.id)["text"]
    await harness.command(DESIGNER, f"/activate {token}", chat=GROUP)  # designer cannot activate
    assert "requires the Team Lead role" in bot.last(GROUP.id)["text"]
    await harness.command(LEAD, "/activate MG-WRONG-TOKEN", chat=GROUP)
    assert "Invalid or expired token" in bot.last(GROUP.id)["text"]
    await harness.command(LEAD, f"/activate {token.lower()}", chat=GROUP)
    assert "now an authorised MG Group" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        assert group_service.is_group_authorised(s, GROUP.id)
        assert group_service.find_valid_token(s, token) is None
        assert not group_service.stale_unauthorised_chats(s, 0)
    await harness.command(LEAD, f"/activate {token}", chat=GROUP)
    assert "already an authorised" in bot.last(GROUP.id)["text"]


@pytest.mark.asyncio
async def test_auto_authorise_when_token_holder_adds_bot(harness):
    bot = harness.bot
    await harness.command(LEAD, "/creategroup")
    await harness.chat_member(GROUP, LEAD, "left", "member")
    assert "now an authorised MG Group" in bot.last(GROUP.id)["text"]
    # bot removed → group revoked; re-adding without a token asks for one
    await harness.chat_member(GROUP, LEAD, "member", "left")
    with session_scope() as s:
        assert not group_service.is_group_authorised(s, GROUP.id)
    await harness.chat_member(GROUP, LEAD, "left", "member")
    assert "not yet an authorised" in bot.last(GROUP.id)["text"]


@pytest.mark.asyncio
async def test_admin_can_revoke_group_and_manage_users(harness, authorised_group):
    bot = harness.bot
    await harness.command(ADMIN, "/groups")
    buttons = harness.buttons(bot.last_markup(ADMIN.id))
    assert any(d == f"ad:g:{GROUP.id}:revoke" for _, d in buttons)
    await harness.press(ADMIN, f"ad:g:{GROUP.id}:revoke")
    q = await harness.press(ADMIN, f"ad:g:{GROUP.id}:revoke2")
    assert "Group revoked" in q.edits[-1]["text"] and bot.left == [GROUP.id]
    with session_scope() as s:
        assert not group_service.is_group_authorised(s, GROUP.id)
    await harness.command(ADMIN, "/users")
    q = await harness.press(ADMIN, f"ad:u:{DESIGNER.id}")
    assert "Role: Designer" in q.edits[-1]["text"]
    q = await harness.press(ADMIN, f"ad:u:{DESIGNER.id}:role:TEAM_LEAD")
    assert "Role: Team Lead" in q.edits[-1]["text"]
    q = await harness.press(ADMIN, f"ad:u:{DESIGNER.id}:revoke")
    assert "Status: REVOKED" in q.edits[-1]["text"]
    q = await harness.press(ADMIN, f"ad:u:{DESIGNER.id}:restore")
    assert "Status: ACTIVE" in q.edits[-1]["text"]
    q = await harness.press(ADMIN, f"ad:u:{ADMIN.id}:revoke")
    assert q.answers[-1] == ("The Super Admin cannot be revoked.", True)


@pytest.mark.asyncio
async def test_setpassword_flow(harness):
    bot = harness.bot
    await harness.command(ADMIN, "/setpassword")
    update = await harness.text(ADMIN, "short")
    assert update.message.deleted and "at least 8 characters" in bot.last(ADMIN.id)["text"]
    await harness.text(ADMIN, "brand-new-password")
    assert "Access password updated" in bot.last(ADMIN.id)["text"]
    with session_scope() as s:
        assert user_service.verify_access_password(s, "brand-new-password")


# ----------------------------------------------------------------------------------------
# Project wizard and management
# ----------------------------------------------------------------------------------------


async def run_wizard(harness, *, with_meta: bool = False, name: str = "Easter Opening 2026", collection: str | None = None):
    bot = harness.bot
    await harness.command(LEAD, "/newproject")
    assert "Where should it live" in bot.last(LEAD.id)["text"]
    if collection is None:
        await harness.press(LEAD, "nw:col:none")
    else:
        buttons = harness.buttons(bot.last_markup(LEAD.id))
        existing = [d for t, d in buttons if t == f"📂 {collection}"]
        if existing:
            await harness.press(LEAD, existing[0])
        else:
            await harness.press(LEAD, "nw:col:new")
            await harness.text(LEAD, collection)
    assert "Send the <b>project name</b>" in bot.last(LEAD.id)["text"]
    await harness.text(LEAD, name)
    assert "Which assets" in bot.last(LEAD.id)["text"]
    q = await harness.press(LEAD, "nw:decl:TIMELINE")
    assert any(t.startswith("✅ Timeline") for t, _ in harness.buttons(q.edits[-1]["reply_markup"]))
    await harness.press(LEAD, "nw:decl:CONTIN_VIDEOS")
    q = await harness.press(LEAD, "nw:decl:done")  # exactly one group → auto-linked → metadata question
    assert "Add metadata now" in q.edits[-1]["text"]
    if with_meta:
        await harness.press(LEAD, "nw:meta:yes")
        assert "Event" in bot.last(LEAD.id)["text"]
        await harness.text(LEAD, "Easter Service")
        await harness.text(LEAD, "-")  # collection
        await harness.text(LEAD, "Worship")  # ministry
        await harness.text(LEAD, "-")  # style
        await harness.text(LEAD, "gold, white")  # colours
        await harness.text(LEAD, "worship, gold, particles")  # tags
        await harness.text(LEAD, "Opening loop with particles")  # description
        assert "Who is working" in bot.last(LEAD.id)["text"]
        markup = bot.last_markup(LEAD.id)
    else:
        q = await harness.press(LEAD, "nw:meta:skip")
        markup = q.edits[-1]["reply_markup"]
    assert any(d == f"nw:asg:{DESIGNER.id}" for _, d in harness.buttons(markup))
    await harness.press(LEAD, f"nw:asg:{DESIGNER.id}")
    q = await harness.press(LEAD, "nw:asg:done")
    assert "Ready to create" in q.edits[-1]["text"] and "Dee Signer" in q.edits[-1]["text"]
    q = await harness.press(LEAD, "nw:confirm")
    return q


@pytest.mark.asyncio
async def test_collections_group_sub_projects_in_one_folder(harness, authorised_group, drive):
    bot = harness.bot
    q = await run_wizard(harness, name="Opening", collection="BF")
    assert "Archive created" in q.edits[-1]["text"]
    q = await run_wizard(harness, name="Worship", collection="BF")  # second one picks the existing collection button
    assert "Archive created" in q.edits[-1]["text"]
    with session_scope() as s:
        projects = {p.name: p for p in project_service.list_projects(s)}
        assert set(projects) == {"Opening", "Worship"}
        for p in projects.values():
            assert p.full_name == f"BF / {p.name}" and p.collection == "BF" and p.collection_folder.name == "BF"
        assert drive.path_of(projects["Opening"].folder("ae").drive_id) == "Archive Root/BF/Opening/Working File/AE"
        assert drive.path_of(projects["Worship"].folder("timeline_hap").drive_id) == "Archive Root/BF/Worship/Final Render/Timeline/Hap/Hap Alpha"
        assert [f.name for f in drive.list_children(drive.ROOT_ID)] == ["BF"]  # one collection folder, nothing else at top level
        assert sorted(f.name for f in drive.list_children(projects["Opening"].collection_folder.drive_id)) == ["Opening", "Worship"]
    # announcements, lists and search use the full name
    assert "New archive: BF / Worship" in bot.last(GROUP.id)["text"]
    await harness.command(LEAD, "/projects")
    assert {t for t, _ in harness.buttons(bot.last_markup(LEAD.id)) if "/" in t} == {"🟢 BF / Opening", "🟢 BF / Worship"}
    await harness.command(DESIGNER, "/search bf")
    assert "2 results" in bot.texts(DESIGNER.id)[-3] and "BF / " in bot.last(DESIGNER.id)["text"]
    # the same project name is allowed in another location, but not twice inside BF
    q = await run_wizard(harness, name="Opening", collection=None)
    assert "Archive created" in q.edits[-1]["text"]
    await harness.command(LEAD, "/newproject")
    buttons = harness.buttons(bot.last_markup(LEAD.id))
    await harness.press(LEAD, [d for t, d in buttons if t == "📂 BF"][0])
    await harness.text(LEAD, "opening")
    assert "already exists in “BF”" in bot.last(LEAD.id)["text"]
    await harness.command(LEAD, "/cancel")
    # a collection folder that already exists on Drive is re-used, and the collection name is not editable as metadata
    existing = drive.create_folder("Christmas", drive.ROOT_ID)
    await run_wizard(harness, name="Intro", collection="Christmas")
    with session_scope() as s:
        p = [p for p in project_service.list_projects(s) if p.name == "Intro"][0]
        assert p.collection_folder.drive_id == existing.id
        pid = p.id
    await harness.press(LEAD, f"pj:{pid}:meta")
    await harness.press(LEAD, f"pj:{pid}:mf:collection")
    await harness.text(LEAD, "Something else")
    assert "comes from that folder" in bot.last(LEAD.id)["text"]


@pytest.mark.asyncio
async def test_wizard_creates_folders_and_announces(harness, authorised_group, drive):
    bot = harness.bot
    q = await run_wizard(harness, with_meta=True)
    assert "Archive created" in q.edits[-1]["text"]
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        assert p.status == ProjectStatus.ACTIVE and p.has_timeline and p.has_contin_videos and not p.has_psd
        assert p.mg_group_chat_id == GROUP.id and p.tag_names == ["gold", "particles", "worship"]
        assert p.event == "Easter Service" and p.ministry == "Worship" and p.colours == "gold, white"
        assert [a.user_id for a in p.assignments] == [DESIGNER.id]
        assert p.creator == "Lee Lead" and p.year == 2026
        assert drive.path_of(p.folder("timeline_hap").drive_id) == "Archive Root/Easter Opening 2026/Final Render/Timeline/Hap/Hap Alpha"
    announcement = bot.last(GROUP.id)["text"]
    assert "New archive: Easter Opening 2026" in announcement and "Dee Signer" in announcement and "Contin Lyrics" not in announcement
    assert harness.user_data[LEAD.id].get("wizard") is None
    # menu buttons present
    labels = [t for t, _ in harness.buttons(q.edits[-1]["reply_markup"])]
    assert "🔎 Check progress" in labels and "📁 Open in Google Drive" in labels


@pytest.mark.asyncio
async def test_wizard_cancel_discards_draft(harness):
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Temp Project")
    await harness.command(LEAD, "/cancel")
    with session_scope() as s:
        assert project_service.list_projects(s) == []
    # duplicate name rejection keeps the prompt alive
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "x")
    assert "between 2 and 100" in harness.bot.last(LEAD.id)["text"]
    await harness.text(LEAD, "Valid Name")
    assert "Which assets" in harness.bot.last(LEAD.id)["text"]
    await harness.press(LEAD, "nw:cancel")
    with session_scope() as s:
        assert project_service.list_projects(s) == []


@pytest.mark.asyncio
async def test_progress_check_reminder_verify_and_reopen(harness, authorised_group, drive):
    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        folders = {f.key: f.drive_id for f in p.folders}
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "Incomplete" in q.edits[-1]["text"] and q.edits[-1]["text"].count("❌") == 6
    # reminder goes to the group and mentions the designer
    q = await harness.press(LEAD, f"pj:{pid}:remind")
    assert "Reminder posted" in q.edits[-1]["text"]
    assert bot.last(GROUP.id)["text"].startswith("⏰") and "Dee Signer" in bot.last(GROUP.id)["text"]
    # verify refused while incomplete
    q = await harness.press(LEAD, f"pj:{pid}:verify")
    assert "Not ready" in q.edits[-1]["text"]
    # upload everything
    for key in ("fonts", "ae", "timeline_prores", "timeline_hap", "contin_prores", "contin_hap"):
        drive.put_file(folders[key], f"{key}.bin")
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "Ready for verification" in q.edits[-1]["text"] and "MG Group has been notified" in q.edits[-1]["text"]
    assert "Ready for Team Lead verification" in bot.last(GROUP.id)["text"]
    # group /status shows complete
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert "awaiting Team Lead verification" in bot.last(GROUP.id)["text"]
    # verify → archived, announced
    q = await harness.press(LEAD, f"pj:{pid}:verify")
    assert "Mark it as" in q.edits[-1]["text"]
    q = await harness.press(LEAD, f"pj:{pid}:verify2")
    assert "Archived." in q.edits[-1]["text"]
    assert "verified and archived" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.status == ProjectStatus.ARCHIVED and p.verified_by == LEAD.id
    # archived project is not touched by scans; reopen works
    q = await harness.press(LEAD, f"pj:{pid}:reopen")
    assert "Reopened" in q.edits[-1]["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).status == ProjectStatus.ACTIVE
    # projects list shows it
    await harness.command(LEAD, "/projects")
    assert any(d == f"pj:{pid}:menu" for _, d in harness.buttons(bot.last_markup(LEAD.id)))


@pytest.mark.asyncio
async def test_metadata_assignment_declaration_and_group_menus(harness, authorised_group, drive):
    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    # metadata edit via prompt
    await harness.press(LEAD, f"pj:{pid}:meta")
    await harness.press(LEAD, f"pj:{pid}:mf:tags")
    assert "Send the new value" in bot.last(LEAD.id)["text"]
    await harness.text(LEAD, "Worship, Gold")
    assert "set to: worship, gold" in bot.last(LEAD.id)["text"]
    await harness.press(LEAD, f"pj:{pid}:mf:year")
    await harness.text(LEAD, "abc")
    assert "Year must be" in bot.last(LEAD.id)["text"]
    await harness.text(LEAD, "2027")
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.tag_names == ["gold", "worship"] and p.year == 2027
    # per-category assignment
    q = await harness.press(LEAD, f"pj:{pid}:assign")
    assert any(d == f"pj:{pid}:asgcat:TIMELINE" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    assert not any(d == f"pj:{pid}:asgcat:PSD" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    await harness.press(LEAD, f"pj:{pid}:asgcat:TIMELINE")
    q = await harness.press(LEAD, f"pj:{pid}:asg:TIMELINE:{LEAD.id}")
    assert q.answers[-1][0] == "Assigned"
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert {(a.user_id, a.category) for a in p.assignments} == {(DESIGNER.id, AssetCategory.ALL), (LEAD.id, AssetCategory.TIMELINE)}
    # enabling PSD later creates the folder on Drive
    await harness.press(LEAD, f"pj:{pid}:decl")
    q = await harness.press(LEAD, f"pj:{pid}:dt:PSD")
    assert q.answers[-1][0] == "Folder created on Drive"
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.has_psd and drive.path_of(p.folder("psd").drive_id).endswith("Working File/PSD")
    # group relink + announce
    q = await harness.press(LEAD, f"pj:{pid}:group")
    await harness.press(LEAD, f"pj:{pid}:grp:none")
    q = await harness.press(LEAD, f"pj:{pid}:announce")
    assert q.answers[-1] == ("Link an MG Group first (💬 MG group).", True)
    await harness.press(LEAD, f"pj:{pid}:grp:{GROUP.id}")
    q = await harness.press(LEAD, f"pj:{pid}:announce")
    assert q.answers[-1][0] == "📣 Announcement posted."
    assert "New archive" in bot.last(GROUP.id)["text"]
    # the bot no longer in the group → graceful failure
    bot.fail_chats.add(GROUP.id)
    q = await harness.press(LEAD, f"pj:{pid}:announce")
    assert "Could not post" in q.answers[-1][0]


# ----------------------------------------------------------------------------------------
# Search & previews (private only)
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_results_and_preview_links(harness, authorised_group, drive, settings):
    bot = harness.bot
    await run_wizard(harness, with_meta=True)
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        # two generated previews and one still pending
        s.add(PreviewAsset(project_id=pid, category=AssetCategory.TIMELINE, source_key="timeline_prores", source_drive_id="src1", source_name="Clouds.mov", preview_name="Clouds.mp4", preview_drive_id="pv1", preview_link="https://drive.google.com/file/d/pv1/view", size_bytes=7_000_000, status=PreviewStatus.READY))
        s.add(PreviewAsset(project_id=pid, category=AssetCategory.TIMELINE, source_key="timeline_prores", source_drive_id="src2", source_name="Cross.mov", preview_name="Cross.mp4", preview_drive_id="pv2", preview_link="https://drive.google.com/file/d/pv2/view", size_bytes=90_000_000, status=PreviewStatus.READY))
        s.add(PreviewAsset(project_id=pid, category=AssetCategory.TIMELINE, source_key="timeline_prores", source_drive_id="src3", source_name="Later.mov", status=PreviewStatus.PENDING))
    await harness.command(DESIGNER, "/search worship, gold")
    texts = bot.texts(DESIGNER.id)
    assert "1 result" in texts[-2] and "🎬 <b>Easter Opening 2026</b>" in texts[-1] and "2 previews available" in texts[-1]
    buttons = harness.buttons(bot.last_markup(DESIGNER.id))
    assert [t for t, _ in buttons] == ["▶️ Preview", "📁 Open Archive", "ℹ️ Details"]
    assert buttons[1][1].startswith("https://drive.google.com/drive/folders/")
    await harness.command(DESIGNER, "/search worship, nothing")
    assert "No projects match" in bot.last(DESIGNER.id)["text"]
    # details
    await harness.press(DESIGNER, f"sr:{pid}:details")
    assert "Declared assets" in bot.last(DESIGNER.id)["text"]
    # preview list: Drive links only, nothing uploaded to Telegram
    await harness.press(DESIGNER, f"sr:{pid}:prev")
    previews = harness.buttons(bot.last_markup(DESIGNER.id))
    assert previews == [("▶️ Clouds.mp4 (6.7 MB)", "https://drive.google.com/file/d/pv1/view"), ("▶️ Cross.mp4 (85.8 MB)", "https://drive.google.com/file/d/pv2/view")]
    assert bot.videos == []
    # a project with exactly one preview gets a direct link message
    with session_scope() as s:
        s.delete(s.query(PreviewAsset).filter_by(preview_name="Cross.mp4").one())
    await harness.press(DESIGNER, f"sr:{pid}:prev")
    assert "Clouds.mp4" in bot.last(DESIGNER.id)["text"]
    assert harness.buttons(bot.last_markup(DESIGNER.id)) == [("▶️ Play preview on Google Drive", "https://drive.google.com/file/d/pv1/view")]
    assert bot.videos == []
    # search prompt when no terms given
    await harness.command(DESIGNER, "/search")
    assert "Send your search terms" in bot.last(DESIGNER.id)["text"]
    await harness.text(DESIGNER, "gold")
    assert "1 result" in bot.texts(DESIGNER.id)[-2]


@pytest.mark.asyncio
async def test_search_pagination(harness, authorised_group, settings, drive):
    bot = harness.bot
    settings.search_page_size = 2
    with session_scope() as s:
        for i in range(5):
            p = project_service.create_draft(s, f"Gold Project {i}", LEAD.id, "Lead", 2020 + i)
            await project_service.provision_folders(s, p, drive, settings)
    await harness.command(DESIGNER, "/search gold")
    assert "5 results" in bot.texts(DESIGNER.id)[-4]
    assert "Showing 2 of 5" in bot.last(DESIGNER.id)["text"]
    await harness.press(DESIGNER, "sp:1")
    assert "Showing 4 of 5" in bot.last(DESIGNER.id)["text"]
    await harness.press(DESIGNER, "sp:2")
    assert "Gold Project 0" in bot.last(DESIGNER.id)["text"]  # oldest year last
    assert bot.last_markup(DESIGNER.id) is not None and "More" not in str(bot.last_markup(DESIGNER.id))


@pytest.mark.asyncio
async def test_group_migration_on_send_and_admin_restore(harness, authorised_group, drive):
    """A basic group upgraded to a supergroup: sends to the old id raise ChatMigrated → records are re-keyed."""
    from telegram.error import ChatMigrated

    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    new_id = -1001234567890
    original = bot.send_message

    async def migrating_send(chat_id, text, reply_markup=None, **kw):
        if chat_id == GROUP.id:
            raise ChatMigrated(new_id)
        return await original(chat_id, text, reply_markup=reply_markup, **kw)

    bot.send_message = migrating_send  # type: ignore[method-assign]
    q = await harness.press(LEAD, f"pj:{pid}:announce")
    assert q.answers[-1][0] == "📣 Announcement posted."
    assert bot.last(new_id)["text"].startswith("📦")
    with session_scope() as s:
        assert group_service.is_group_authorised(s, new_id) and not group_service.get_group(s, GROUP.id)
        assert project_service.get_project(s, pid).mg_group_chat_id == new_id
    bot.send_message = original  # type: ignore[method-assign]
    # Super Admin revokes → Team Lead token cannot re-activate → Super Admin restores
    await harness.press(ADMIN, f"ad:g:{new_id}:revoke")
    await harness.press(ADMIN, f"ad:g:{new_id}:revoke2")
    await harness.command(LEAD, "/creategroup")
    token = bot.last(LEAD.id)["text"].split("<code>")[1].split("</code>")[0]
    new_group = FakeChat(new_id, "supergroup", "MG Team")
    await harness.command(LEAD, f"/activate {token}", chat=new_group)
    assert "revoked by the Super Admin" in bot.last(new_id)["text"]
    await harness.chat_member(new_group, LEAD, "left", "member")
    assert "revoked by the Super Admin" in bot.last(new_id)["text"]
    q = await harness.press(ADMIN, f"ad:g:{new_id}:restore")
    assert "Group restored" in q.edits[-1]["text"]
    with session_scope() as s:
        assert group_service.is_group_authorised(s, new_id)
    await harness.chat_member(new_group, LEAD, "left", "member")
    assert "Reconnected" in bot.last(new_id)["text"]


@pytest.mark.asyncio
async def test_lockout_alerts_super_admin(harness):
    bot = harness.bot
    await harness.command(STRANGER, "/start")
    for _ in range(5):
        await harness.text(STRANGER, "nope")
    alert = bot.last(ADMIN.id)["text"]
    assert "Login lockout" in alert and str(STRANGER.id) in alert


@pytest.mark.asyncio
async def test_html_special_characters_in_names_are_escaped(harness):
    spiky = FakeUser(5001, "Dee <3", "<MG>", username="spiky")
    with session_scope() as s:
        user_service.register_designer(s, spiky.id, spiky.full_name, spiky.username)
    await harness.command(spiky, "/start")
    assert "Dee &lt;3 &lt;MG&gt;" in harness.bot.last(spiky.id)["text"]
    await harness.command(spiky, "/whoami")
    assert "&lt;MG&gt;" in harness.bot.last(spiky.id)["text"]


@pytest.mark.asyncio
async def test_projects_cannot_be_linked_to_unauthorised_chats(harness, authorised_group):
    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    q = await harness.press(LEAD, f"pj:{pid}:grp:-100999")  # crafted callback_data for a random chat
    assert q.answers[-1] == ("That chat is not an authorised MG Group.", True)
    with session_scope() as s:
        assert project_service.get_project(s, pid).mg_group_chat_id == GROUP.id
    # once the Super Admin revokes the group, nothing is posted there any more
    await harness.press(ADMIN, f"ad:g:{GROUP.id}:revoke")
    await harness.press(ADMIN, f"ad:g:{GROUP.id}:revoke2")
    n = len(bot.texts(GROUP.id))
    q = await harness.press(LEAD, f"pj:{pid}:announce")
    assert "Could not post" in q.answers[-1][0] and len(bot.texts(GROUP.id)) == n
    q = await harness.press(LEAD, f"pj:{pid}:menu")
    assert "revoked" in q.edits[-1]["text"]


@pytest.mark.asyncio
async def test_pending_prompt_is_cancelled_by_buttons_and_expires(harness, authorised_group):
    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
        original = project_service.get_project(s, pid).description
    await harness.press(LEAD, f"pj:{pid}:meta")
    await harness.press(LEAD, f"pj:{pid}:mf:description")
    await harness.press(LEAD, f"pj:{pid}:menu")  # navigating away cancels the pending text step
    await harness.text(LEAD, "worship gold")  # ...so this is a search, not a description
    assert "result" in bot.last(LEAD.id)["text"] or "No projects match" in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).description == original
    # stale prompts expire instead of swallowing later text
    await harness.command(ADMIN, "/setpassword")
    harness.user_data[ADMIN.id]["prompt"]["created_at"] -= 10_000
    await harness.text(ADMIN, "not-a-password-really")
    assert "expired" in bot.last(ADMIN.id)["text"]
    with session_scope() as s:
        assert user_service.verify_access_password(s, PASSWORD)


@pytest.mark.asyncio
async def test_double_tap_create_archive_provisions_once(harness, authorised_group, drive):
    import asyncio
    import time

    # Make Drive slow enough that the second tap arrives while the first is still provisioning.
    original_create = drive.create_folder

    def slow_create(name, parent_id):
        time.sleep(0.01)
        return original_create(name, parent_id)

    drive.create_folder = slow_create  # type: ignore[method-assign]
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Double Tap")
    await harness.press(LEAD, "nw:decl:done")
    await harness.press(LEAD, "nw:meta:skip")
    await harness.press(LEAD, "nw:asg:done")
    results = await asyncio.gather(harness.press(LEAD, "nw:confirm"), harness.press(LEAD, "nw:confirm"))
    texts = [r.edits[-1]["text"] for r in results]
    assert sum("Archive created" in t for t in texts) == 1
    # The loser either waited on the lock ("already created") or arrived after the wizard closed ("expired").
    assert sum(("already created" in t) or ("wizard has expired" in t) for t in texts) == 1
    roots = [f for f in drive.all_files() if f.name.startswith("Double Tap") and f.is_folder and f.parents == (drive.ROOT_ID,)]
    assert len(roots) == 1
    with session_scope() as s:
        assert len(project_service.list_projects(s)) == 1


@pytest.mark.asyncio
async def test_declaration_toggle_survives_drive_failure(harness, authorised_group, monkeypatch):
    from mg_archive_bot.services.drive import DriveError

    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id

    async def failing(*args, **kwargs):
        raise DriveError("Drive is down")

    monkeypatch.setattr(project_service, "ensure_folders", failing)
    await harness.press(LEAD, f"pj:{pid}:decl")
    q = await harness.press(LEAD, f"pj:{pid}:dt:PSD")
    assert len(q.answers) == 1 and q.answers[0][1] is True and "Drive is down" in q.answers[0][0]
    with session_scope() as s:
        assert project_service.get_project(s, pid).has_psd is True


@pytest.mark.asyncio
async def test_drive_outage_does_not_change_status_or_remind(harness, authorised_group, drive):
    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        drive.delete(p.folder("fonts").drive_id)  # listing this folder now raises DriveError
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "could not check" in q.edits[-1]["text"] and "status unchanged" in q.edits[-1]["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).status == ProjectStatus.ACTIVE
    n = len(bot.texts(GROUP.id))
    q = await harness.press(LEAD, f"pj:{pid}:remind")
    assert "no reminder sent" in q.edits[-1]["text"] and len(bot.texts(GROUP.id)) == n
    await harness.command(LEAD, "/remind", chat=GROUP)
    assert "Could not check Google Drive" in bot.last(GROUP.id)["text"]


@pytest.mark.asyncio
async def test_unregistered_senders_are_answered_once_per_minute(harness):
    bot = harness.bot
    for _ in range(5):
        await harness.text(STRANGER, "hello?")
    assert len(bot.texts(STRANGER.id)) == 1
    for _ in range(3):
        await harness.command(STRANGER, "/search gold")  # guarded command → also throttled
    assert len(bot.texts(STRANGER.id)) == 1
    harness.bot_data.pop("limiters", None)
    await harness.text(STRANGER, "hello again")
    assert len(bot.texts(STRANGER.id)) == 2


@pytest.mark.asyncio
async def test_global_password_breaker_pauses_registration_and_alerts(harness, settings):
    bot = harness.bot
    settings.password_breaker_failures = 3
    attackers = [FakeUser(9000 + i, f"Bot{i}") for i in range(3)]
    for a in attackers:
        await harness.command(a, "/start")
        await harness.text(a, "guess")
    assert "Registration paused" in bot.last(ADMIN.id)["text"]
    assert sum("Registration paused" in t for t in bot.texts(ADMIN.id)) == 1
    victim = FakeUser(9999, "Late")
    await harness.command(victim, "/start")
    assert "temporarily paused" in bot.last(victim.id)["text"]
    # genuine users who already registered are unaffected
    await harness.command(DESIGNER, "/search gold")
    assert "No projects match" in bot.last(DESIGNER.id)["text"]


@pytest.mark.asyncio
async def test_group_status_reuses_recent_check(harness, authorised_group):
    from mg_archive_bot.models import ValidationRun

    await run_wizard(harness)
    await harness.command(DESIGNER, "/status", chat=GROUP)
    with session_scope() as s:
        runs = s.query(ValidationRun).count()
    await harness.command(DESIGNER, "/status", chat=GROUP)  # within the cooldown → no new Drive scan
    with session_scope() as s:
        assert s.query(ValidationRun).count() == runs
    assert harness.bot.last(GROUP.id)["text"].startswith("📊")
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/status", chat=GROUP)
    with session_scope() as s:
        assert s.query(ValidationRun).count() == runs + 1
