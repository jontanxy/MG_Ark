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
    # the Super Admin is told, with a one-tap promotion
    notice = bot.last(ADMIN.id)
    assert "New user registered" in notice["text"] and "Ran Dom" in notice["text"] and str(STRANGER.id) in notice["text"]
    assert ("⬆️ Make Team Lead", f"ad:u:{STRANGER.id}:role:TEAM_LEAD") in harness.buttons(notice["reply_markup"])
    q = await harness.press(ADMIN, f"ad:u:{STRANGER.id}:role:TEAM_LEAD")
    assert "Role: Team Lead" in q.edits[-1]["text"]
    with session_scope() as s:
        assert user_service.get_user(s, STRANGER.id).role == Role.TEAM_LEAD
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
    assert update.message.deleted and "at least 12 characters" in bot.last(ADMIN.id)["text"]
    await harness.text(ADMIN, "change-me-please")
    assert "placeholder" in bot.last(ADMIN.id)["text"]
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
    q = await harness.press(LEAD, "nw:decl:done")  # the group is always chosen explicitly, never auto-linked
    assert "Which MG Group" in q.edits[-1]["text"]
    assert any(d == f"nw:grp:{GROUP.id}" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    q = await harness.press(LEAD, f"nw:grp:{GROUP.id}")
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
        assert [f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder] == ["BF"]  # one collection folder at top level
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
    assert [t for t, _ in buttons] == ["▶️ Preview", "📁 Open Archive", "ℹ️ Details", "📂 Files"]
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
    await harness.press(LEAD, f"nw:grp:{GROUP.id}")
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
    live = harness.bot.last(GROUP.id)
    assert live["text"].startswith("📊")
    await harness.command(DESIGNER, "/status", chat=GROUP)  # within the cooldown → no new scan, no re-post
    with session_scope() as s:
        assert s.query(ValidationRun).count() == runs
    assert "refreshed less than a minute ago" in harness.bot.last(GROUP.id)["text"]
    assert (GROUP.id, live["message_id"]) not in harness.bot.deleted
    for _ in range(5):  # the same person asking again and again is answered at most 3 times per 10 min
        await harness.command(DESIGNER, "/status", chat=GROUP)
    assert sum("refreshed less than" in t for t in harness.bot.texts(GROUP.id)) == 3
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/status", chat=GROUP)
    with session_scope() as s:
        assert s.query(ValidationRun).count() == runs + 1


@pytest.mark.asyncio
async def test_project_index_sheet_is_kept_in_sync(harness, authorised_group, drive):
    from mg_archive_bot.bot.actions import flush_sheet_syncs

    bot = harness.bot
    await run_wizard(harness, with_meta=True)
    ctx = harness.ctx(LEAD)
    await flush_sheet_syncs(ctx)
    sheet_id = next(iter(harness.sheets.books))
    from mg_archive_bot.services import tracking

    col = tracking.HEADERS.index
    rows = harness.sheets.get_values(sheet_id, "'Projects'!A:AA")
    assert rows[0] == tracking.HEADERS and len(rows) == 2 and len(rows[1]) == len(tracking.HEADERS)
    assert rows[1][2] == "Easter Opening 2026" and rows[1][3] == "Active" and rows[1][4] == "Lee Lead" and rows[1][6] == "Easter Service" and "Dee Signer" in rows[1][col("Assigned")]
    assert [rows[1][col(h)] for h in ("Timeline", "Contin Videos", "Contin Lyrics", "Titlebars", "PSD")] == ["Yes", "Yes", "No", "No", "No"]
    assert drive.path_of(sheet_id) == "Archive Root/MG Archive Index"
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        folders = {f.key: f.drive_id for f in p.folders}
    # a metadata edit and a status change update the same row
    await harness.press(LEAD, f"pj:{pid}:meta")
    await harness.press(LEAD, f"pj:{pid}:mf:style")
    await harness.text(LEAD, "cinematic")
    for key in ("fonts", "ae", "timeline_prores", "timeline_hap", "contin_prores", "contin_hap"):
        drive.put_file(folders[key], f"{key}.bin")
    await harness.press(LEAD, f"pj:{pid}:verify")
    await harness.press(LEAD, f"pj:{pid}:verify2")
    await flush_sheet_syncs(ctx)
    rows = harness.sheets.get_values(sheet_id, "'Projects'!A:AA")
    assert len(rows) == 2 and rows[1][3] == "Archived" and rows[1][8] == "cinematic" and rows[1][col("Verified by")] == "Lee Lead"
    # /sheet gives the link; /sheet rebuild rewrites it
    await harness.command(LEAD, "/sheet")
    assert f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" in bot.last(LEAD.id)["text"] and "1 project" in bot.last(LEAD.id)["text"]
    harness.sheets.books[sheet_id]["Projects"] = [["garbage"]]
    await harness.command(LEAD, "/sheet rebuild")
    assert "Index rebuilt: 1 project" in bot.last(LEAD.id)["text"]
    rows = harness.sheets.get_values(sheet_id, "'Projects'!A:AA")
    assert rows[0] == tracking.HEADERS and rows[1][2] == "Easter Opening 2026"
    await harness.command(DESIGNER, "/sheet")
    assert "requires the Team Lead role" in bot.last(DESIGNER.id)["text"]


@pytest.mark.asyncio
async def test_sheet_failure_never_breaks_the_flow(harness, authorised_group, monkeypatch):
    from mg_archive_bot.bot.actions import flush_sheet_syncs
    from mg_archive_bot.services import tracking

    def boom(*args, **kwargs):
        raise RuntimeError("Sheets API disabled")

    monkeypatch.setattr(tracking, "upsert_row", boom)
    q = await run_wizard(harness)
    assert "Archive created" in q.edits[-1]["text"]  # the project was created regardless
    await flush_sheet_syncs(harness.ctx(LEAD))
    alerts = [t for t in harness.bot.texts(ADMIN.id) if "index sheet could not be updated" in t]
    assert len(alerts) == 1 and "Sheets API disabled" in alerts[0]


@pytest.mark.asyncio
async def test_revoke_trashes_folder_and_restore_brings_it_back(harness, authorised_group, drive):
    from mg_archive_bot.models import PreviewAsset

    bot = harness.bot
    await run_wizard(harness, with_meta=True)
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid, root_id = p.id, p.drive_root_id
        drive.put_file(p.folder("fonts").drive_id, "Font.otf")
        s.add(PreviewAsset(project_id=pid, category=AssetCategory.TIMELINE, source_key="timeline_prores", source_drive_id="src1", source_name="Clouds.mov", preview_name="Clouds.mp4", preview_drive_id="pv1", preview_link="https://drive/pv1", status=PreviewStatus.READY))
    await harness.press(LEAD, f"pj:{pid}:check")
    q = await harness.press(LEAD, f"pj:{pid}:revoke")
    assert "Revoke Easter Opening 2026?" in q.edits[-1]["text"] and "1 file" in q.edits[-1]["text"]
    assert any(d == f"pj:{pid}:revoke2" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    q = await harness.press(LEAD, f"pj:{pid}:revoke2")
    assert "Revoked" in q.edits[-1]["text"] and any(t.endswith("Restore project") for t, _ in harness.buttons(q.edits[-1]["reply_markup"]))
    assert drive.get_file(root_id).trashed
    assert "cancelled by" in bot.last(GROUP.id)["text"] and "moved to the trash" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.status == ProjectStatus.CANCELLED and p.cancelled_by == LEAD.id and p.previews == []
        assert project_service.list_projects(s, (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION)) == []
    # hidden from designers' search, listed under archived & cancelled, skipped by the scan job
    await harness.command(DESIGNER, "/search worship")
    assert "No projects match" in bot.last(DESIGNER.id)["text"]
    await harness.command(LEAD, "/projects")
    assert not any(d == f"pj:{pid}:menu" for _, d in harness.buttons(bot.last_markup(LEAD.id)))
    q = await harness.press(LEAD, "pl:0:archived")
    assert any(t.startswith("🗑") and d == f"pj:{pid}:menu" for t, d in harness.buttons(q.edits[-1]["reply_markup"]))
    from mg_archive_bot.bot.jobs import scan_job

    await scan_job(harness.ctx(LEAD))
    with session_scope() as s:
        assert project_service.get_project(s, pid).status == ProjectStatus.CANCELLED
    # revoking twice / revoking an archived project is refused
    q = await harness.press(LEAD, f"pj:{pid}:revoke")
    assert q.answers[-1][1] is True and "Only active" in q.answers[-1][0]
    # restore: folder comes back, tracking resumes
    q = await harness.press(LEAD, f"pj:{pid}:restore")
    assert "Restored" in q.edits[-1]["text"]
    assert not drive.get_file(root_id).trashed and "restored" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.status == ProjectStatus.ACTIVE and p.cancelled_by is None
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "Fonts</a> (1 file)" in q.edits[-1]["text"]
    # designers cannot revoke
    q = await harness.press(DESIGNER, f"pj:{pid}:revoke2")
    assert "Team Lead" in q.answers[-1][0]


@pytest.mark.asyncio
async def test_revoke_removes_the_index_sheet_row(harness, authorised_group, drive):
    from mg_archive_bot.bot.actions import flush_sheet_syncs

    ctx = harness.ctx(LEAD)
    await run_wizard(harness, name="First")
    await run_wizard(harness, name="Second")
    await run_wizard(harness, name="Third")
    await flush_sheet_syncs(ctx)
    sheet_id = next(iter(harness.sheets.books))
    assert [r[2] for r in harness.sheets.get_values(sheet_id, "'Projects'!A:AA")[1:]] == ["First", "Second", "Third"]
    with session_scope() as s:
        second = [p for p in project_service.list_projects(s) if p.name == "Second"][0].id
    await harness.press(LEAD, f"pj:{second}:revoke")
    await harness.press(LEAD, f"pj:{second}:revoke2")
    await flush_sheet_syncs(ctx)
    assert [r[2] for r in harness.sheets.get_values(sheet_id, "'Projects'!A:Y")[1:]] == ["First", "Third"]  # row gone, Third moved up
    await harness.command(LEAD, "/sheet rebuild")
    assert [r[2] for r in harness.sheets.get_values(sheet_id, "'Projects'!A:Y")[1:]] == ["First", "Third"]
    await harness.press(LEAD, f"pj:{second}:restore")
    await flush_sheet_syncs(ctx)
    assert [r[2] for r in harness.sheets.get_values(sheet_id, "'Projects'!A:Y")[1:]] == ["First", "Third", "Second"]


@pytest.mark.asyncio
async def test_wizard_lets_team_lead_pick_between_groups(harness, authorised_group):
    """Two projects, two chats: each archive is announced only in the chat chosen for it."""
    bot = harness.bot
    second = FakeChat(-100600, "supergroup", "Alabaster Team")
    with session_scope() as s:
        token = group_service.create_token(s, LEAD.id, 24)
        group_service.authorise_group(s, second.id, second.title, token)
    for name, chat in (("Keep On", GROUP), ("Alabaster Jar", second)):
        await harness.command(LEAD, "/newproject")
        await harness.press(LEAD, "nw:col:none")
        await harness.text(LEAD, name)
        q = await harness.press(LEAD, "nw:decl:done")
        labels = {t: d for t, d in harness.buttons(q.edits[-1]["reply_markup"])}
        assert labels["💬 MG Team"] == f"nw:grp:{GROUP.id}" and labels["💬 Alabaster Team"] == f"nw:grp:{second.id}"
        assert "/creategroup" in q.edits[-1]["text"]
        await harness.press(LEAD, f"nw:grp:{chat.id}")
        await harness.press(LEAD, "nw:meta:skip")
        q = await harness.press(LEAD, "nw:asg:done")
        assert chat.title in q.edits[-1]["text"]
        n_first, n_second = len(bot.texts(GROUP.id)), len(bot.texts(second.id))
        await harness.press(LEAD, "nw:confirm")
        assert (len(bot.texts(GROUP.id)) - n_first, len(bot.texts(second.id)) - n_second) == ((1, 0) if chat is GROUP else (0, 1))
        assert f"New archive: {name}" in bot.last(chat.id)["text"]
    with session_scope() as s:
        linked = {p.name: p.mg_group_chat_id for p in project_service.list_projects(s)}
        assert linked == {"Keep On": GROUP.id, "Alabaster Jar": second.id}


@pytest.mark.asyncio
async def test_wizard_without_any_group_explains_how_to_add_one(harness):
    bot = harness.bot
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Lonely Project")
    q = await harness.press(LEAD, "nw:decl:done")
    assert "No MG Group is authorised yet" in q.edits[-1]["text"] and "/creategroup" in q.edits[-1]["text"]
    assert [d for _, d in harness.buttons(q.edits[-1]["reply_markup"])] == ["nw:grp:none"]
    q = await harness.press(LEAD, "nw:grp:none")
    assert "Add metadata now" in q.edits[-1]["text"]
    await harness.press(LEAD, "nw:meta:skip")
    q = await harness.press(LEAD, "nw:asg:done")
    assert "nothing will be announced" in q.edits[-1]["text"]
    q = await harness.press(LEAD, "nw:confirm")
    assert "No MG Group linked" in q.edits[-1]["text"]
    assert all(m["chat_id"] > 0 for m in bot.sent)  # nothing was posted to any group


@pytest.mark.asyncio
async def test_group_rename_is_reflected_in_selections(harness, authorised_group):
    from mg_archive_bot.bot.jobs import refresh_group_titles_job

    bot = harness.bot

    def stored_title(chat_id=GROUP.id):
        with session_scope() as s:
            return group_service.get_group(s, chat_id).title

    # 1. Telegram's rename service message updates the stored name immediately...
    await harness.rename_group(GROUP, LEAD, "MG Team (Renamed)")
    assert stored_title() == "MG Team (Renamed)"
    # ...and the wizard's group choice shows it
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Renamed Check")
    q = await harness.press(LEAD, "nw:decl:done")
    assert ("💬 MG Team (Renamed)", f"nw:grp:{GROUP.id}") in harness.buttons(q.edits[-1]["reply_markup"])
    await harness.command(LEAD, "/cancel")
    # 2. any command used inside the group syncs the name Telegram attaches to the update
    await harness.command(DESIGNER, "/status", chat=FakeChat(GROUP.id, "supergroup", "MG Team v3"))
    assert stored_title() == "MG Team v3"
    # 3. the periodic refresh asks Telegram directly (covers renames made while the bot was offline)
    bot.chat_titles[GROUP.id] = "MG Team v4"
    with session_scope() as s:
        token = group_service.create_token(s, LEAD.id, 24)
        group_service.authorise_group(s, -100777, "Ghost", token)  # bot cannot reach this chat any more
    await refresh_group_titles_job(harness.ctx(LEAD))
    assert stored_title() == "MG Team v4" and stored_title(-100777) == "Ghost"
    # a rename of a chat the bot does not know is ignored
    await harness.rename_group(FakeChat(-100999, "supergroup", "Random"), STRANGER, "Random 2")
    with session_scope() as s:
        assert group_service.get_group(s, -100999) is None


@pytest.mark.asyncio
async def test_live_status_message_is_edited_in_place(harness, authorised_group, drive):
    from mg_archive_bot.bot.jobs import scan_job

    bot = harness.bot
    ctx = harness.ctx(LEAD)
    await run_wizard(harness)
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid, folders = p.id, {f.key: f.drive_id for f in p.folders}

    def live_id():
        with session_scope() as s:
            return project_service.get_project(s, pid).status_message_id

    n = len(bot.texts(GROUP.id))
    # background scans never add messages to the chat
    await scan_job(ctx)
    assert len(bot.texts(GROUP.id)) == n and bot.edited == [] and live_id() is None
    # /status creates the one live status message
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert len(bot.texts(GROUP.id)) == n + 1
    first = bot.last(GROUP.id)["message_id"]
    assert live_id() == first and "6 of 6 required folders still empty" in bot.last(GROUP.id)["text"]
    # a designer uploads; the next background scan edits that message instead of posting
    drive.put_file(folders["fonts"], "Font.otf")
    await scan_job(ctx)
    assert len(bot.texts(GROUP.id)) == n + 1
    assert bot.edited[-1]["message_id"] == first and "Fonts</a> (1 file)" in bot.edited[-1]["text"]
    assert "5 of 6 required folders still empty" in bot.messages[(GROUP.id, first)]["text"]
    # a private "Check progress" also refreshes the group's message silently
    drive.put_file(folders["ae"], "project.aep")
    await harness.press(LEAD, f"pj:{pid}:check")
    assert len(bot.texts(GROUP.id)) == n + 1 and "4 of 6" in bot.messages[(GROUP.id, first)]["text"]
    # /status again moves the message to the bottom: old copy deleted, one new message
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert (GROUP.id, first) in bot.deleted and len(bot.texts(GROUP.id)) == n + 2
    second = bot.last(GROUP.id)["message_id"]
    assert live_id() == second
    # when the old copy is too old to delete, it is marked outdated instead
    bot.undeletable.add((GROUP.id, second))
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert "Outdated" in bot.messages[(GROUP.id, second)]["text"] and len(bot.texts(GROUP.id)) == n + 3
    third = bot.last(GROUP.id)["message_id"]
    # if a group admin deletes the live message, scans stay silent and the next /status re-creates it
    bot.messages.pop((GROUP.id, third))
    drive.put_file(folders["timeline_prores"], "loop.mov")
    await scan_job(ctx)
    assert len(bot.texts(GROUP.id)) == n + 3 and live_id() is None
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert len(bot.texts(GROUP.id)) == n + 4 and live_id() == bot.last(GROUP.id)["message_id"]
    # completion: the live message shows the new state, and the READY notice is a separate ping
    for key in ("timeline_hap", "contin_prores", "contin_hap"):
        drive.put_file(folders[key], f"{key}.bin")
    await scan_job(ctx)
    assert "awaiting Team Lead verification" in bot.messages[(GROUP.id, live_id())]["text"]
    assert "Ready for Team Lead verification" in bot.last(GROUP.id)["text"]
    await harness.press(LEAD, f"pj:{pid}:verify")
    await harness.press(LEAD, f"pj:{pid}:verify2")
    assert "Archive verified and closed" in bot.messages[(GROUP.id, live_id())]["text"]
    # relinking to another group forgets the message that lives in the old chat
    other = FakeChat(-100600, "supergroup", "Other Team")
    with session_scope() as s:
        token = group_service.create_token(s, LEAD.id, 24)
        group_service.authorise_group(s, other.id, other.title, token)
    await harness.press(LEAD, f"pj:{pid}:group")
    await harness.press(LEAD, f"pj:{pid}:grp:{other.id}")
    assert live_id() is None


@pytest.mark.asyncio
async def test_file_listing_from_menu_search_and_group(harness, authorised_group, drive):
    bot = harness.bot
    await run_wizard(harness, with_meta=True)
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid, folders = p.id, {f.key: f.drive_id for f in p.folders}
    drive.put_file(folders["fonts"], "Gotham.otf", size=1_000)
    # Team Lead: project menu button
    q = await harness.press(LEAD, f"pj:{pid}:menu")
    assert ("📂 Files", f"pj:{pid}:files") in harness.buttons(q.edits[-1]["reply_markup"])
    q = await harness.press(LEAD, f"pj:{pid}:files")
    assert q.answers[-1][0] == "Reading Google Drive…"
    listing = bot.last(LEAD.id)["text"]
    assert listing.startswith("📂 <b>Easter Opening 2026</b> — 1 file · 1000 B") and "Gotham.otf · 1000 B" in listing
    # Designer: from a search card
    await harness.command(DESIGNER, "/search worship")
    assert ("📂 Files", f"sr:{pid}:files") in harness.buttons(bot.last_markup(DESIGNER.id))
    await harness.press(DESIGNER, f"sr:{pid}:files")
    assert "Gotham.otf" in bot.last(DESIGNER.id)["text"]
    # Drive is re-read at most once per cooldown window
    drive.put_file(folders["ae"], "opening.aep", size=2_000)
    await harness.press(DESIGNER, f"sr:{pid}:files")
    assert "opening.aep" not in bot.last(DESIGNER.id)["text"]
    q = await harness.press(DESIGNER, f"sr:{pid}:files")  # third request inside a minute: budget exhausted
    assert q.answers[-1] == ("Please wait a minute before requesting another file listing.", True)
    harness.bot_data.pop("file_listings", None)
    harness.bot_data.pop("limiters", None)
    await harness.press(DESIGNER, f"sr:{pid}:files")
    assert "opening.aep" in bot.last(DESIGNER.id)["text"]
    # group /files: any authorised member, authorised group only
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/files", chat=GROUP)
    assert "opening.aep" in bot.last(GROUP.id)["text"] and "Open project folder" in bot.last(GROUP.id)["text"]
    await harness.command(STRANGER, "/files", chat=GROUP)
    assert "not an authorised user" in bot.last(GROUP.id)["text"]
    await harness.command(DESIGNER, "/files")
    assert "only works inside an MG Group" in bot.last(DESIGNER.id)["text"]
    # a revoked project's folder is in the trash: no listing offered
    await harness.press(LEAD, f"pj:{pid}:revoke")
    await harness.press(LEAD, f"pj:{pid}:revoke2")
    q = await harness.press(LEAD, f"pj:{pid}:files")
    assert q.answers[-1] == ("This project's folder is in the Drive trash.", True)
    harness.bot_data.pop("limiters", None)
    await harness.command(DESIGNER, "/files", chat=GROUP)
    assert "No open archives" in bot.last(GROUP.id)["text"]


@pytest.mark.asyncio
async def test_lights_role_is_view_only(harness, authorised_group, drive):
    bot = harness.bot
    lights = FakeUser(6001, "Lucy", "Lights", username="lucy")
    with session_scope() as s:
        user_service.register_designer(s, lights.id, lights.full_name, lights.username)
    # Super Admin assigns the role from the user card
    q = await harness.press(ADMIN, f"ad:u:{lights.id}")
    labels = [t for t, _ in harness.buttons(q.edits[-1]["reply_markup"])]
    assert "💡 Make Lights" in labels and "⬆️ Make Team Lead" in labels and "🎨 Make Designer" not in labels
    q = await harness.press(ADMIN, f"ad:u:{lights.id}:role:LIGHTS")
    assert "Role: Lights" in q.edits[-1]["text"]
    assert [t for t, _ in harness.buttons(q.edits[-1]["reply_markup"])][:2] == ["⬆️ Make Team Lead", "🎨 Make Designer"]
    with session_scope() as s:
        assert user_service.get_user(s, lights.id).role == Role.LIGHTS
    # can search, see details, files and previews
    await run_wizard(harness, with_meta=True)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
        s.add(PreviewAsset(project_id=pid, category=AssetCategory.TIMELINE, source_key="timeline_prores", source_drive_id="src1", source_name="Loop.mov", preview_name="Loop.mp4", preview_drive_id="pv1", preview_link="https://drive.google.com/file/d/pv1/view", size_bytes=5_000_000, status=PreviewStatus.READY))
    await harness.command(lights, "/start")
    assert "Lights access" in bot.last(lights.id)["text"] and "/newproject" not in bot.last(lights.id)["text"]
    await harness.command(lights, "/search worship")
    assert "1 result" in bot.texts(lights.id)[-2]
    assert harness.buttons(bot.last_markup(lights.id)) == [("▶️ Preview", f"sr:{pid}:prev")]  # no archive link, details or files
    await harness.press(lights, f"sr:{pid}:prev")
    assert harness.buttons(bot.last_markup(lights.id)) == [("▶️ Play preview on Google Drive", "https://drive.google.com/file/d/pv1/view")]
    n = len(bot.texts(lights.id))
    for action in ("details", "files"):  # crafted button data is refused
        q = await harness.press(lights, f"sr:{pid}:{action}")
        assert q.answers[-1] == ("Lights access is preview-only.", True)
    assert len(bot.texts(lights.id)) == n
    await harness.command(lights, "/files", chat=GROUP)
    assert "preview-only" in bot.last(GROUP.id)["text"]
    await harness.command(lights, "/status", chat=GROUP)  # progress is fine: the group sees it anyway
    assert bot.last(GROUP.id)["text"].startswith("📊")
    # designers keep the full card
    await harness.command(DESIGNER, "/search worship")
    assert [t for t, _ in harness.buttons(bot.last_markup(DESIGNER.id))] == ["▶️ Preview", "📁 Open Archive", "ℹ️ Details", "📂 Files"]
    # nothing else: Team Lead / admin commands and buttons are refused
    for cmd in ("/newproject", "/projects", "/creategroup", "/sheet"):
        await harness.command(lights, cmd)
        assert "requires the Team Lead role" in bot.last(lights.id)["text"]
    await harness.command(lights, "/users")
    assert "requires the Super Admin role" in bot.last(lights.id)["text"]
    q = await harness.press(lights, f"pj:{pid}:verify2")
    assert "Team Lead" in q.answers[-1][0]
    # never offered for assignment, and cannot be assigned even with crafted data
    q = await harness.press(LEAD, f"pj:{pid}:assign")
    await harness.press(LEAD, f"pj:{pid}:asgcat:ALL")
    offered = [d for _, d in harness.buttons(bot.last(LEAD.id)["reply_markup"]) if d.startswith(f"pj:{pid}:asg:ALL:")]
    assert f"pj:{pid}:asg:ALL:{DESIGNER.id}" in offered and f"pj:{pid}:asg:ALL:{lights.id}" not in offered
    q = await harness.press(LEAD, f"pj:{pid}:asg:ALL:{lights.id}")
    assert q.answers[-1][1] is True and "cannot be assigned" in q.answers[-1][0]
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Second Song")
    await harness.press(LEAD, "nw:decl:done")
    await harness.press(LEAD, f"nw:grp:{GROUP.id}")
    q = await harness.press(LEAD, "nw:meta:skip")
    assert not any(d == f"nw:asg:{lights.id}" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    await harness.command(LEAD, "/cancel")
    # the registration notice offers Lights directly
    await harness.command(STRANGER, "/start")
    await harness.text(STRANGER, PASSWORD)
    assert ("💡 Make Lights", f"ad:u:{STRANGER.id}:role:LIGHTS") in harness.buttons(bot.last(ADMIN.id)["reply_markup"])


@pytest.mark.asyncio
async def test_project_lead_ownership(harness, authorised_group, drive):
    """One Team Lead per project: the creator by default, chosen by the Super Admin otherwise; only the lead (or the Super Admin) manages it."""
    from mg_archive_bot.bot.actions import flush_sheet_syncs

    bot = harness.bot
    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
    # a Team Lead who creates a project is its lead; the announcement names them
    await run_wizard(harness)
    assert "Lead:</b> <a href=\"tg://user?id=2001\">Lee Lead</a>" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        assert p.lead_id == LEAD.id and project_service.can_manage(p.lead, p)
        folders = {f.key: f.drive_id for f in p.folders}
    # another Team Lead only gets the read-only menu and every management action is refused
    q = await harness.press(other, f"pj:{pid}:menu")
    labels = [t for t, _ in harness.buttons(q.edits[-1]["reply_markup"])]
    assert "🔎 Check progress" in labels and "👥 Assign designers" not in labels and "✅ Verify & archive" not in labels and "👑 Project lead" not in labels
    assert "Lead:</b> Lee Lead" in q.edits[-1]["text"]
    for data in (f"pj:{pid}:assign", f"pj:{pid}:mf:style", f"pj:{pid}:dt:PSD", f"pj:{pid}:grp:none", f"pj:{pid}:verify2", f"pj:{pid}:revoke2", f"pj:{pid}:setlead:{other.id}", f"pj:{pid}:remind"):
        q = await harness.press(other, data)
        assert q.answers[-1] == ("Only this project's lead (or the Super Admin) can do that.", True), data
    from mg_archive_bot.bot.access import set_prompt

    set_prompt(harness.ctx(other), "meta_value", project_id=pid, field="style")
    await harness.text(other, "hijacked")
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.style == "" and p.lead_id == LEAD.id and p.has_psd is False
    # the lead and the Super Admin can; the Super Admin cannot be made lead, a Team Lead can
    q = await harness.press(LEAD, f"pj:{pid}:lead")
    assert any(d == f"pj:{pid}:setlead:{other.id}" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    assert not any(d == f"pj:{pid}:setlead:{ADMIN.id}" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    q = await harness.press(ADMIN, f"pj:{pid}:setlead:{ADMIN.id}")
    assert "Super Admin cannot lead" in q.answers[-1][0]
    q = await harness.press(ADMIN, f"pj:{pid}:setlead:{DESIGNER.id}")
    assert "must hold the Team Lead role" in q.answers[-1][0]
    q = await harness.press(ADMIN, f"pj:{pid}:setlead:{other.id}")
    assert q.answers[-1][0] == "Lead: Olga Other" and "You are now the lead" in bot.last(other.id)["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).lead_id == other.id
    q = await harness.press(LEAD, f"pj:{pid}:assign")  # the former lead is now read-only
    assert q.answers[-1][1] is True and "Only this project's lead" in q.answers[-1][0]
    # DMs go to the lead, not the creator
    for key in ("fonts", "ae", "timeline_prores", "timeline_hap", "contin_prores", "contin_hap"):
        drive.put_file(folders[key], f"{key}.bin")
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert "Ready for Team Lead verification" in bot.last(other.id)["text"]
    assert not any("Ready for Team Lead verification" in t for t in bot.texts(LEAD.id))
    # the index sheet carries the lead
    await flush_sheet_syncs(harness.ctx(ADMIN))
    sheet_id = next(iter(harness.sheets.books))
    assert harness.sheets.get_values(sheet_id, "'Projects'!A:Z")[1][4] == "Olga Other"
    # revoking the lead frees the project; the Super Admin is told which projects need a new lead
    q = await harness.press(ADMIN, f"ad:u:{other.id}:revoke")
    assert "Projects without a lead" in q.edits[-1]["text"] and "Easter Opening 2026" in q.edits[-1]["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).lead_id is None
    q = await harness.press(LEAD, f"pj:{pid}:assign")  # nobody but the Super Admin until reassigned
    assert "Only this project's lead" in q.answers[-1][0]
    q = await harness.press(ADMIN, f"pj:{pid}:menu")
    assert "none — set one" in q.edits[-1]["text"]
    # demoting a lead to Designer also releases their projects
    await harness.press(ADMIN, f"pj:{pid}:setlead:{LEAD.id}")
    q = await harness.press(ADMIN, f"ad:u:{LEAD.id}:role:DESIGNER")
    assert "Projects without a lead" in q.edits[-1]["text"]


@pytest.mark.asyncio
async def test_super_admin_wizard_requires_choosing_a_lead(harness, authorised_group, drive):
    bot = harness.bot
    await harness.command(ADMIN, "/newproject")
    await harness.press(ADMIN, "nw:col:none")
    await harness.text(ADMIN, "Admin Made")
    q = await harness.press(ADMIN, "nw:decl:done")
    assert "Who is the <b>project lead</b>" in q.edits[-1]["text"]
    buttons = harness.buttons(q.edits[-1]["reply_markup"])
    assert ("👑 Lee Lead", f"nw:lead:{LEAD.id}") in buttons and not any(d.endswith(f":{ADMIN.id}") or d.endswith(f":{DESIGNER.id}") for _, d in buttons)
    await harness.press(ADMIN, f"nw:lead:{DESIGNER.id}")  # crafted: not a Team Lead
    assert "must hold the Team Lead role" in bot.last(ADMIN.id)["text"]
    q = await harness.press(ADMIN, f"nw:lead:{LEAD.id}")
    assert "Which MG Group" in q.edits[-1]["text"]
    await harness.press(ADMIN, f"nw:grp:{GROUP.id}")
    await harness.press(ADMIN, "nw:meta:skip")
    q = await harness.press(ADMIN, "nw:asg:done")
    assert "Lead:</b> Lee Lead" in q.edits[-1]["text"]
    await harness.press(ADMIN, "nw:confirm")
    with session_scope() as s:
        p = [p for p in project_service.list_projects(s) if p.name == "Admin Made"][0]
        assert p.lead_id == LEAD.id and p.created_by == ADMIN.id
    assert "Lead:</b>" in bot.last(GROUP.id)["text"] and "Lee Lead" in bot.last(GROUP.id)["text"]
    # with no Team Leads at all, the Super Admin is told to promote someone first
    with session_scope() as s:
        user_service.set_role(s, LEAD.id, Role.DESIGNER, SUPER_ADMIN_ID)
    await harness.command(ADMIN, "/newproject")
    await harness.press(ADMIN, "nw:col:none")
    await harness.text(ADMIN, "No Leads Yet")
    q = await harness.press(ADMIN, "nw:decl:done")
    assert "Promote someone in /users" in q.edits[-1]["text"]
    with session_scope() as s:
        assert not [p for p in project_service.list_projects(s) if p.name == "No Leads Yet"]


@pytest.mark.asyncio
async def test_assignment_list_markers(harness, authorised_group):
    """Team Leads carry ★ and the Super Admin 👁️‍🗨️ in assignment lists; Designers carry nothing."""
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Markers")
    await harness.press(LEAD, "nw:decl:done")
    await harness.press(LEAD, f"nw:grp:{GROUP.id}")
    q = await harness.press(LEAD, "nw:meta:skip")
    labels = {d: t for t, d in harness.buttons(q.edits[-1]["reply_markup"])}
    assert labels[f"nw:asg:{ADMIN.id}"] == "⬜️ Boss 👁️‍🗨️"
    assert labels[f"nw:asg:{LEAD.id}"] == "⬜️ Lee Lead ★"
    assert labels[f"nw:asg:{DESIGNER.id}"] == "⬜️ Dee Signer"
    assert "👁️‍🗨️ = Super Admin" in q.edits[-1]["text"]
    await harness.command(LEAD, "/cancel")


@pytest.mark.asyncio
async def test_rename_project(harness, authorised_group, drive):
    from mg_archive_bot.bot.actions import flush_sheet_syncs
    from mg_archive_bot.services.drive import DriveError

    bot = harness.bot
    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
    await run_wizard(harness, with_meta=True)
    await run_wizard(harness, name="Taken Name")
    with session_scope() as s:
        p = [p for p in project_service.list_projects(s) if p.name == "Easter Opening 2026"][0]
        pid, root_id = p.id, p.drive_root_id
    # only the lead (or Super Admin) may rename
    q = await harness.press(other, f"pj:{pid}:rename")
    assert q.answers[-1] == ("Only this project's lead (or the Super Admin) can do that.", True)
    # validation keeps the prompt alive
    await harness.press(LEAD, f"pj:{pid}:rename")
    assert "Send the new project name" in bot.last(LEAD.id)["text"]
    for bad, msg in (("x", "between 2 and 100"), ("taken name", "already exists"), ("Easter Opening 2026", "already the project"), ("Bad <b>", "cannot contain")):
        await harness.text(LEAD, bad)
        assert msg in bot.last(LEAD.id)["text"], bad
    # Drive refusal keeps the old name
    original = drive.rename

    def refuse(file_id, name):
        raise DriveError("quota")

    drive.rename = refuse  # type: ignore[method-assign]
    await harness.text(LEAD, "Easter Opening 2027")
    assert "refused to rename" in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        assert project_service.get_project(s, pid).name == "Easter Opening 2026"
    drive.rename = original  # type: ignore[method-assign]
    # success: project, Drive folder, group notice, live status and sheet all follow
    await harness.command(DESIGNER, "/status", chat=GROUP)  # creates the live status message
    await harness.press(LEAD, f"pj:{pid}:rename")
    await harness.text(LEAD, "  Easter   Opening 2027 ")
    assert "Renamed <b>Easter Opening 2026</b> → <b>Easter Opening 2027</b>" in bot.last(LEAD.id)["text"]
    assert drive.get_file(root_id).name == "Easter Opening 2027" and drive.path_of(root_id) == "Archive Root/Easter Opening 2027"
    assert "is now <b>Easter Opening 2027</b>" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.name == "Easter Opening 2027" and p.folder("root").name == "Easter Opening 2027" and p.drive_root_id == root_id
        assert "Easter Opening 2027" in bot.messages[(GROUP.id, p.status_message_id)]["text"]
    await flush_sheet_syncs(harness.ctx(LEAD))
    sheet_id = next(iter(harness.sheets.books))
    assert [r[2] for r in harness.sheets.get_values(sheet_id, "'Projects'!A:Z")[1:]] == ["Easter Opening 2027", "Taken Name"]
    # a same-named folder already on Drive gets a suffix, the project name itself stays exact
    drive.create_folder("Clash", drive.ROOT_ID)
    await harness.press(LEAD, f"pj:{pid}:rename")
    await harness.text(LEAD, "Clash")
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.name == "Clash" and drive.get_file(root_id).name == "Clash (2)"
    # cancelled projects cannot be renamed
    await harness.press(LEAD, f"pj:{pid}:revoke")
    await harness.press(LEAD, f"pj:{pid}:revoke2")
    q = await harness.press(LEAD, f"pj:{pid}:rename")
    assert q.answers[-1][1] is True and "Cancelled projects" in q.answers[-1][0]


def _plain(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", html)


@pytest.mark.asyncio
async def test_titlebars_can_be_declared_in_the_wizard(harness, authorised_group, drive):
    from mg_archive_bot.bot.actions import flush_sheet_syncs
    from mg_archive_bot.services import tracking

    bot = harness.bot
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:none")
    await harness.text(LEAD, "Conference Titles")
    assert "Which assets" in bot.last(LEAD.id)["text"]
    offered = harness.buttons(bot.last_markup(LEAD.id))
    assert offered == [
        ("⬜️ Timeline", "nw:decl:TIMELINE"),
        ("⬜️ Contin Videos", "nw:decl:CONTIN_VIDEOS"),
        ("⬜️ Contin Lyrics", "nw:decl:CONTIN_LYRICS"),
        ("⬜️ Titlebars", "nw:decl:TITLEBARS"),
        ("⬜️ PSD", "nw:decl:PSD"),
        ("Continue ➡️", "nw:decl:done"),
    ]
    q = await harness.press(LEAD, "nw:decl:TITLEBARS")
    assert ("✅ Titlebars", "nw:decl:TITLEBARS") in harness.buttons(q.edits[-1]["reply_markup"])
    q = await harness.press(LEAD, "nw:decl:TITLEBARS")  # toggles off again
    assert ("⬜️ Titlebars", "nw:decl:TITLEBARS") in harness.buttons(q.edits[-1]["reply_markup"])
    await harness.press(LEAD, "nw:decl:TITLEBARS")
    await harness.press(LEAD, "nw:decl:done")
    await harness.press(LEAD, f"nw:grp:{GROUP.id}")
    await harness.press(LEAD, "nw:meta:skip")
    await harness.press(LEAD, f"nw:asg:{DESIGNER.id}")
    q = await harness.press(LEAD, "nw:asg:done")
    assert "<b>Declared assets:</b> Titlebars" in q.edits[-1]["text"]
    q = await harness.press(LEAD, "nw:confirm")
    assert "Archive created" in q.edits[-1]["text"]
    announcement = bot.last(GROUP.id)["text"]
    assert "• Final Render / Titlebars — " in announcement and "Titlebars /" not in announcement
    assert "Timeline" not in announcement
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        assert p.has_titlebars and not p.has_timeline and p.asset_types == "Working Files, Titlebars"
        assert drive.path_of(p.folder("titlebars").drive_id) == "Archive Root/Conference Titles/Final Render/Titlebars"
        assert drive.list_children(p.folder("titlebars").drive_id) == []  # one folder, nothing inside
        assert sorted(f.name for f in drive.list_children(p.folder("final_render").drive_id)) == ["Contin Lyrics", "Contin Videos", "Timeline", "Titlebars"]
        folders = {f.key: f.drive_id for f in p.folders}
    await flush_sheet_syncs(harness.ctx(LEAD))
    sheet_id = next(iter(harness.sheets.books))
    rows = harness.sheets.get_values(sheet_id, f"'Projects'!A:{tracking.LAST_COL}")
    assert rows[0] == tracking.HEADERS and rows[1][tracking.HEADERS.index("Titlebars")] == "Yes"
    assert rows[1][tracking.HEADERS.index("Asset types")] == "Working Files, Titlebars"
    # the whole life cycle works with Titlebars as the only declared asset
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "❌ Final Render / Titlebars  👤" in _plain(q.edits[-1]["text"]) and q.edits[-1]["text"].count("❌") == 3
    assert f'<a href="https://drive.google.com/drive/folders/{folders["titlebars"]}">' in q.edits[-1]["text"]
    for key in ("fonts", "ae", "titlebars"):
        drive.put_file(folders[key], f"{key}.mov")
    drive.put_file(folders["titlebars"], "second.png")
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "✅ Final Render / Titlebars (2 files)" in _plain(q.edits[-1]["text"])
    assert "Ready for verification" in q.edits[-1]["text"]
    await harness.press(LEAD, f"pj:{pid}:verify")
    await harness.press(LEAD, f"pj:{pid}:verify2")
    with session_scope() as s:
        assert project_service.get_project(s, pid).status == ProjectStatus.ARCHIVED


@pytest.mark.asyncio
async def test_titlebars_can_be_added_to_an_existing_project(harness, authorised_group, drive):
    from mg_archive_bot.bot.actions import flush_sheet_syncs
    from mg_archive_bot.services import tracking

    await run_wizard(harness)  # Timeline + Contin Videos, as before
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid = p.id
        assert not p.has_titlebars and p.folder("titlebars") is None
        assert "Titlebars" not in [f.name for f in drive.list_children(p.folder("final_render").drive_id)]
    q = await harness.press(LEAD, f"pj:{pid}:assign")
    assert not any(d == f"pj:{pid}:asgcat:TITLEBARS" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    q = await harness.press(LEAD, f"pj:{pid}:decl")
    assert ("⬜️ Titlebars", f"pj:{pid}:dt:TITLEBARS") in harness.buttons(q.edits[-1]["reply_markup"])
    q = await harness.press(LEAD, f"pj:{pid}:dt:TITLEBARS")
    assert q.answers[-1][0] == "Folder created on Drive"
    assert ("✅ Titlebars", f"pj:{pid}:dt:TITLEBARS") in harness.buttons(q.edits[-1]["reply_markup"])
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.has_titlebars and p.asset_types == "Working Files, Timeline, Contin Videos, Titlebars"
        assert drive.path_of(p.folder("titlebars").drive_id) == "Archive Root/Easter Opening 2026/Final Render/Titlebars"
        titlebars = [f.name for f in drive.list_children(p.folder("final_render").drive_id) if f.name == "Titlebars"]
        assert titlebars == ["Titlebars"]
    # off and on again never duplicates the folders
    await harness.press(LEAD, f"pj:{pid}:dt:TITLEBARS")
    q = await harness.press(LEAD, f"pj:{pid}:dt:TITLEBARS")
    assert q.answers[-1][0] == "Saved"
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert [f.name for f in drive.list_children(p.folder("final_render").drive_id)].count("Titlebars") == 1
        assert drive.list_children(p.folder("titlebars").drive_id) == []
    # it can now be assigned on its own, and the assignee is named next to the missing Titlebars folder
    q = await harness.press(LEAD, f"pj:{pid}:assign")
    assert ("Titlebars (0)", f"pj:{pid}:asgcat:TITLEBARS") in harness.buttons(q.edits[-1]["reply_markup"])
    q = await harness.press(LEAD, f"pj:{pid}:asgcat:TITLEBARS")
    assert "assign for <b>Titlebars</b>" in q.edits[-1]["text"]
    q = await harness.press(LEAD, f"pj:{pid}:asg:TITLEBARS:{LEAD.id}")
    assert q.answers[-1][0] == "Assigned"
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert (LEAD.id, AssetCategory.TITLEBARS) in {(a.user_id, a.category) for a in p.assignments}
    q = await harness.press(LEAD, f"pj:{pid}:check")
    lines = _plain(q.edits[-1]["text"]).splitlines()
    assert [line for line in lines if "Titlebars" in line] == ["❌ Final Render / Titlebars  👤 Dee Signer, Lee Lead"]
    assert [line for line in lines if "Contin Videos / ProRes" in line] == ["❌ Final Render / Contin Videos / ProRes 4444  👤 Dee Signer"]
    await flush_sheet_syncs(harness.ctx(LEAD))
    sheet_id = next(iter(harness.sheets.books))
    row = harness.sheets.get_values(sheet_id, f"'Projects'!A:{tracking.LAST_COL}")[1]
    assert row[tracking.HEADERS.index("Titlebars")] == "Yes" and row[tracking.HEADERS.index("PSD")] == "No"
    # a designer cannot switch declarations, and junk button data is ignored
    q = await harness.press(DESIGNER, f"pj:{pid}:dt:TITLEBARS")
    with session_scope() as s:
        assert project_service.get_project(s, pid).has_titlebars is True
    q = await harness.press(LEAD, f"pj:{pid}:dt:TITLEBARZ")
    assert q.answers[-1] == ("Invalid request.", True)


def _make_legacy_sheet(harness) -> tuple[str, list[list[str]]]:
    """Turn the harness's index sheet into what the previous version wrote (26 columns, no Titlebars), with a
    column of notes that a teammate added to the right of it."""
    from mg_archive_bot.services import tracking

    sheet_id = next(iter(harness.sheets.books))
    skip = tracking.HEADERS.index("Titlebars")
    current = harness.sheets.get_values(sheet_id, "'Projects'!A:AA")
    rows = [[c for i, c in enumerate(r + [""] * (27 - len(r))) if i != skip] for r in current]
    assert rows[0] == tracking.PREVIOUS_HEADERS and all(len(r) == 26 for r in rows)
    notes = ["Notes", *[f"note for {r[2]}" for r in rows[1:]]]
    harness.sheets.books[sheet_id]["Projects"] = [row + [note] for row, note in zip(rows, notes, strict=True)]
    harness.sheets.columns[sheet_id]["Projects"] = 27
    harness.sheets.requests.clear()
    return sheet_id, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("first_action", ["edit", "revoke", "rebuild"])
async def test_index_sheet_of_the_previous_version_is_upgraded_by_the_first_sync(harness, authorised_group, drive, first_action):
    from mg_archive_bot.bot.actions import flush_sheet_syncs
    from mg_archive_bot.services import tracking

    col = tracking.HEADERS.index
    ctx = harness.ctx(LEAD)
    for name in ("First", "Second", "Third"):
        await run_wizard(harness, name=name)
    with session_scope() as s:
        ids = {p.name: p.id for p in project_service.list_projects(s)}
    await harness.press(LEAD, f"pj:{ids['First']}:decl")
    await harness.press(LEAD, f"pj:{ids['First']}:dt:PSD")
    await flush_sheet_syncs(ctx)
    sheet_id, legacy_rows = _make_legacy_sheet(harness)
    assert [r[2] for r in legacy_rows[1:]] == ["First", "Second", "Third"]

    if first_action == "edit":
        await harness.press(LEAD, f"pj:{ids['Second']}:meta")
        await harness.press(LEAD, f"pj:{ids['Second']}:mf:style")
        await harness.text(LEAD, "cinematic")
        expected = ["First", "Second", "Third"]
    elif first_action == "revoke":
        await harness.press(LEAD, f"pj:{ids['Third']}:revoke")
        await harness.press(LEAD, f"pj:{ids['Third']}:revoke2")
        expected = ["First", "Second"]
    else:
        await harness.command(LEAD, "/sheet rebuild")
        expected = ["First", "Second", "Third"]
    await flush_sheet_syncs(ctx)

    # the column was inserted in place: nothing else was re-formatted, and the teammate's column moved along
    assert [next(iter(r)) for r in harness.sheets.requests] == ["insertDimension"]
    assert harness.sheets.sheet_grids(sheet_id) == {"Projects": (0, 28)}
    rows = harness.sheets.get_values(sheet_id, "'Projects'!A:AA")
    assert rows[0] == tracking.HEADERS and [r[2] for r in rows[1:]] == expected
    by_name = {r[2]: r for r in rows[1:]}
    assert [by_name["First"][col(h)] for h in ("Contin Lyrics", "Titlebars", "PSD")] == ["No", "No", "Yes"]
    assert [by_name["Second"][col(h)] for h in ("Contin Lyrics", "Titlebars", "PSD")] == ["No", "No", "No"]
    for row in rows[1:]:
        assert "Dee Signer" in row[col("Assigned")] and row[col("Created by")] == "Lee Lead"
        assert row[col("Drive link")].startswith("https://drive.google.com/") and row[col("MG Group")] == "MG Team"
    if first_action == "edit":
        assert by_name["Second"][col("Style")] == "cinematic"
    notes = [r[27] for r in harness.sheets.get_values(sheet_id, "'Projects'!A:AB")]
    assert notes == ["Notes", "note for First", "note for Second", "note for Third"]
    assert "sheet_failure_reported" not in ctx.bot_data
    # later syncs are ordinary row updates: the layout is not rebuilt or re-formatted again
    harness.sheets.requests.clear()
    await harness.press(LEAD, f"pj:{ids['First']}:decl")
    await harness.press(LEAD, f"pj:{ids['First']}:dt:TITLEBARS")
    await flush_sheet_syncs(ctx)
    rows = harness.sheets.get_values(sheet_id, "'Projects'!A:AA")
    assert [r[2] for r in rows[1:]] == expected and rows[1][col("Titlebars")] == "Yes" and rows[2][col("Titlebars")] == "No"
    assert harness.sheets.requests == []


@pytest.mark.asyncio
async def test_titlebars_folder_is_created_after_a_drive_failure(harness, authorised_group, drive):
    from mg_archive_bot.services.drive import DriveError

    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    real = drive.create_folder
    state = {"down": True}

    def flaky(name, parent_id):
        if state["down"] and name == "Titlebars":
            raise DriveError("Google Drive is unavailable")
        return real(name, parent_id)

    drive.create_folder = flaky
    await harness.press(LEAD, f"pj:{pid}:decl")
    q = await harness.press(LEAD, f"pj:{pid}:dt:PSD")
    assert q.answers[-1][0] == "Folder created on Drive"
    q = await harness.press(LEAD, f"pj:{pid}:dt:TITLEBARS")
    assert q.answers[-1][1] is True and q.answers[-1][0].startswith("Saved, but Drive folder creation failed")
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.has_titlebars and p.folder("psd") is not None and p.folder("titlebars") is None
        final_render = p.folder("final_render").drive_id
    # while Drive is still failing a check reports the folder as missing, without a link, and does not crash
    q = await harness.press(LEAD, f"pj:{pid}:check")
    missing = [line for line in q.edits[-1]["text"].splitlines() if "Titlebars" in line]
    assert len(missing) == 1 and missing[0].startswith("❌ Final Render / Titlebars  👤 ")  # the folder name is plain text: no link yet
    # once Drive works again the next check (or background scan) creates it by itself
    state["down"] = False
    q = await harness.press(LEAD, f"pj:{pid}:check")
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        titlebars = p.folder("titlebars")
        assert titlebars is not None and drive.path_of(titlebars.drive_id).endswith("Easter Opening 2026/Final Render/Titlebars")
        assert [f.name for f in drive.list_children(final_render)].count("Titlebars") == 1
        titlebars_id = titlebars.drive_id
    assert f"folders/{titlebars_id}" in q.edits[-1]["text"]
    drive.put_file(titlebars_id, "Title_01.mov")
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert "✅ Final Render / Titlebars (1 file)" in _plain(q.edits[-1]["text"])
    # closed projects are never touched: even with the folder gone from the records AND from Drive, nothing is
    # looked up or created for an archived or a cancelled project
    from mg_archive_bot.bot import actions

    with session_scope() as s:
        p = project_service.get_project(s, pid)
        drive.delete(titlebars_id)
        for folder in list(p.folders):
            if folder.key == "titlebars":
                p.folders.remove(folder)
        s.commit()
    for status in (ProjectStatus.ARCHIVED, ProjectStatus.CANCELLED, ProjectStatus.DRAFT):
        with session_scope() as s:
            p = project_service.get_project(s, pid)
            p.status = status
            s.commit()
            seen: list[tuple] = []
            real_find, real_create = drive.find_child_folder, drive.create_folder
            drive.find_child_folder = lambda *a, **k: seen.append(("find", a)) or real_find(*a, **k)
            drive.create_folder = lambda *a, **k: seen.append(("create", a)) or real_create(*a, **k)
            try:
                await actions._repair_folders(s, p, drive, harness.bot_data["settings"])
            finally:
                drive.find_child_folder, drive.create_folder = real_find, real_create
            assert seen == [] and p.folder("titlebars") is None, status
            assert "Titlebars" not in [f.name for f in drive.list_children(final_render)], status
    # an open project in the same state is repaired
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        p.status = ProjectStatus.INCOMPLETE
        s.commit()
        await actions._repair_folders(s, p, drive, harness.bot_data["settings"])
        assert p.folder("titlebars") is not None and p.folder("titlebars").drive_id != titlebars_id
        assert [f.name for f in drive.list_children(final_render)].count("Titlebars") == 1


@pytest.mark.asyncio
async def test_project_with_the_earlier_titlebars_layout_follows_the_new_one(harness, authorised_group, drive):
    """What the live project looks like: Titlebars declared while it still had two format folders."""
    from mg_archive_bot.bot import jobs
    from mg_archive_bot.models import ProjectFolder
    from mg_archive_bot.services.drive import folder_link
    from mg_archive_bot.services.validation import latest_report

    bot = harness.bot
    await run_wizard(harness)
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    await harness.press(LEAD, f"pj:{pid}:dt:TITLEBARS")

    def add_format_folders(names=("ProRes 4444", "Hap/Hap Alpha")) -> dict[str, str]:
        with session_scope() as s:
            p = project_service.get_project(s, pid)
            made = {}
            for key, name in zip(("titlebars_prores", "titlebars_hap"), names, strict=True):
                folder = drive.create_folder(name, p.folder("titlebars").drive_id)
                p.folders.append(ProjectFolder(project_id=p.id, key=key, name=name, drive_id=folder.id, link=folder_link(folder.id)))
                made[key] = folder.id
            s.commit()
            return made

    def titlebars_state() -> tuple[list[str], list[str]]:
        with session_scope() as s:
            p = project_service.get_project(s, pid)
            records = sorted(f.key for f in p.folders if f.key.startswith("titlebars"))
            return records, sorted(f.name for f in drive.list_children(p.folder("titlebars").drive_id))

    def removals() -> list[tuple]:
        return [c for c in drive.calls if c[0] in ("delete", "move", "rename")]

    # before any check every view works with the old records
    made = add_format_folders()
    for data in ("menu", "details", "decl", "assign"):  # (the Files view is looked at below: its answer is kept for a minute)
        q = await harness.press(LEAD, f"pj:{pid}:{data}")
        assert q.answers and not any("went wrong" in (a[0] or "") for a in q.answers), data
    # a manual check: the bot follows the new layout, Google Drive is left exactly as it is
    assert titlebars_state() == (["titlebars", "titlebars_hap", "titlebars_prores"], ["Hap/Hap Alpha", "ProRes 4444"])
    q = await harness.press(LEAD, f"pj:{pid}:check")
    assert titlebars_state() == (["titlebars"], ["Hap/Hap Alpha", "ProRes 4444"])
    assert removals() == [] and not any(drive.get_file(i).trashed for i in made.values())
    lines = [line for line in _plain(q.edits[-1]["text"]).splitlines() if "Titlebars" in line]
    assert lines == ["❌ Final Render / Titlebars  👤 Dee Signer"]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert [i.key for i in latest_report(s, p).required_items if i.category == "TITLEBARS"] == ["titlebars"]
    # an upload that was still running when the bot was restarted arrives afterwards: it is there and it counts
    clip = drive.put_file(made["titlebars_hap"], "Titlebars_Song_A_hap.mov")
    await harness.command(DESIGNER, "/status", chat=GROUP)
    assert "✅ Final Render / Titlebars (1 file)" in _plain(bot.last(GROUP.id)["text"])
    assert drive.get_file(clip.id).trashed is False
    await harness.press(ADMIN, f"pj:{pid}:files")
    assert "Titlebars_Song_A_hap.mov" in bot.last(ADMIN.id)["text"]
    # the background scan does the same for a project nobody looks at
    add_format_folders(names=("ProRes 4444 b", "Hap b"))
    await jobs.scan_job(harness.ctx(LEAD))
    assert titlebars_state() == (["titlebars"], ["Hap b", "Hap/Hap Alpha", "ProRes 4444", "ProRes 4444 b"])
    assert removals() == []
    # an archived project keeps its records until it is open again
    add_format_folders(names=("ProRes 4444 c", "Hap c"))
    with session_scope() as s:
        project_service.get_project(s, pid).status = ProjectStatus.ARCHIVED
        s.commit()
    await jobs.scan_job(harness.ctx(LEAD))
    assert titlebars_state()[0] == ["titlebars", "titlebars_hap", "titlebars_prores"]
    q = await harness.press(LEAD, f"pj:{pid}:details")
    assert "Titlebars" in q.edits[-1]["text"]
    await harness.press(LEAD, f"pj:{pid}:reopen")
    await harness.press(LEAD, f"pj:{pid}:check")
    assert titlebars_state()[0] == ["titlebars"] and removals() == []


@pytest.mark.asyncio
async def test_project_can_be_moved_in_and_out_of_collections(harness, authorised_group, drive):
    from mg_archive_bot.bot.actions import flush_sheet_syncs
    from mg_archive_bot.services import collections as collection_service
    from mg_archive_bot.services import search as search_service
    from mg_archive_bot.services import tracking
    from mg_archive_bot.services.drive import DriveError

    bot = harness.bot
    ctx = harness.ctx(LEAD)
    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
    await run_wizard(harness, name="God I'm Just Thankful", collection="Building Fund 2026")
    await run_wizard(harness, name="Opening", collection="Building Fund 2026")
    await run_wizard(harness, name="Opening")
    with session_scope() as s:
        projects = {p.full_name: p for p in project_service.list_projects(s)}
        assert set(projects) == {"Building Fund 2026 / God I'm Just Thankful", "Building Fund 2026 / Opening", "Opening"}
        song = projects["Building Fund 2026 / God I'm Just Thankful"]
        pid, root_id, folders = song.id, song.drive_root_id, {f.key: (f.drive_id, f.link) for f in song.folders}
        top_opening = projects["Opening"].id
        bf_id = song.collection_id
    await flush_sheet_syncs(ctx)
    sheet_id = next(iter(harness.sheets.books))

    def sheet_rows() -> dict[str, list[str]]:
        return {r[0]: r for r in harness.sheets.get_values(sheet_id, f"'Projects'!A:{tracking.LAST_COL}")[1:]}

    def found(term: str) -> set[str]:
        with session_scope() as s:
            return {hit.project.full_name for hit in search_service.search_projects(s, term)}

    await flush_sheet_syncs(ctx)
    assert sheet_rows()[str(pid)][1:3] == ["Building Fund 2026", "God I'm Just Thankful"]
    assert found("building fund") == {"Building Fund 2026 / God I'm Just Thankful", "Building Fund 2026 / Opening"}

    # the option is in the lead's menu only
    q = await harness.press(LEAD, f"pj:{pid}:menu")
    assert ("📂 Collection", f"pj:{pid}:col") in harness.buttons(q.edits[-1]["reply_markup"])
    q = await harness.press(other, f"pj:{pid}:menu")
    assert not any(d == f"pj:{pid}:col" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    for stranger, data in ((other, "col"), (other, "setcol:none"), (DESIGNER, "col"), (DESIGNER, "setcol:none")):
        q = await harness.press(stranger, f"pj:{pid}:{data}")
        assert q.answers[-1][1] is True and ("project's lead" in q.answers[-1][0] or "Team Lead role" in q.answers[-1][0])
    assert drive.path_of(root_id) == "Archive Root/Building Fund 2026/God I'm Just Thankful"

    # 1. take it out of the collection
    await harness.command(DESIGNER, "/status", chat=GROUP)  # creates the live status messages
    q = await harness.press(LEAD, f"pj:{pid}:col")
    assert "is in the collection <b>Building Fund 2026</b>" in q.edits[-1]["text"]
    assert harness.buttons(q.edits[-1]["reply_markup"]) == [
        ("📁 Top level (take it out of the collection)", f"pj:{pid}:setcol:none"),
        ("➕ New collection…", f"pj:{pid}:setcol:new"),
        ("◀️ Back to project", f"pj:{pid}:menu"),
    ]
    q = await harness.press(LEAD, f"pj:{pid}:setcol:none")
    assert q.answers == [(None, False)]  # acknowledged at once; the outcome is shown in the message
    assert _plain(q.edits[-1]["text"]).startswith("✅ God I'm Just Thankful was taken out of Building Fund 2026 and now sits at the top level.")
    assert ("📂 Collection", f"pj:{pid}:col") in harness.buttons(q.edits[-1]["reply_markup"])
    assert drive.path_of(root_id) == "Archive Root/God I'm Just Thankful"
    assert drive.path_of(folders["timeline_prores"][0]) == "Archive Root/God I'm Just Thankful/Final Render/Timeline/ProRes 4444"
    assert _plain(bot.last(GROUP.id)["text"]) == (
        "📂 God I'm Just Thankful was taken out of the collection Building Fund 2026. "
        "The Google Drive folder was moved with it; existing links keep working."
    )
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert (p.collection_id, p.collection, p.full_name, p.drive_root_id) == (None, "", "God I'm Just Thankful", root_id)
        assert {f.key: (f.drive_id, f.link) for f in p.folders} == folders  # the same folders, the same links
        live = bot.messages[(GROUP.id, p.status_message_id)]["text"]
        assert "<b>God I'm Just Thankful</b>" in live and "Building Fund 2026" not in live
        collection = collection_service.get_collection(s, bf_id)
        assert [f.name for f in drive.list_children(collection.drive_id)] == ["Opening"]  # the rest of the collection stays
    await flush_sheet_syncs(ctx)
    assert sheet_rows()[str(pid)][1:3] == ["", "God I'm Just Thankful"]
    assert found("building fund") == {"Building Fund 2026 / Opening"} and found("thankful") == {"God I'm Just Thankful"}

    # 2. put it back
    q = await harness.press(LEAD, f"pj:{pid}:col")
    assert "is at the top level (in no collection)" in q.edits[-1]["text"]
    assert harness.buttons(q.edits[-1]["reply_markup"])[0] == ("📂 Building Fund 2026", f"pj:{pid}:setcol:{bf_id}")
    assert not any(d == f"pj:{pid}:setcol:none" for _, d in harness.buttons(q.edits[-1]["reply_markup"]))
    q = await harness.press(LEAD, f"pj:{pid}:setcol:none")
    assert q.edits[-1]["text"].startswith("❌ It is not in a collection.\n\n📂 <b>God I'm Just Thankful</b> is at the top level")
    assert harness.buttons(q.edits[-1]["reply_markup"])[0] == ("📂 Building Fund 2026", f"pj:{pid}:setcol:{bf_id}")  # choose again
    q = await harness.press(LEAD, f"pj:{pid}:setcol:{bf_id}")
    assert q.answers == [(None, False)] and "is now in <b>Building Fund 2026</b>" in q.edits[-1]["text"]
    assert drive.path_of(root_id) == "Archive Root/Building Fund 2026/God I'm Just Thankful"
    assert "is now part of the collection <b>Building Fund 2026</b>" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert (p.collection_id, p.collection, p.full_name) == (bf_id, "Building Fund 2026", "Building Fund 2026 / God I'm Just Thankful")
        assert "Building Fund 2026 / God I'm Just Thankful" in bot.messages[(GROUP.id, p.status_message_id)]["text"]
    await flush_sheet_syncs(ctx)
    assert sheet_rows()[str(pid)][1] == "Building Fund 2026"
    assert found("building fund") == {"Building Fund 2026 / God I'm Just Thankful", "Building Fund 2026 / Opening"}
    notices = len(bot.texts(GROUP.id))
    q = await harness.press(LEAD, f"pj:{pid}:setcol:{bf_id}")  # a second press: the project menu, not an error
    assert q.edits[-1]["text"].startswith("ℹ️ <b>God I'm Just Thankful</b> is already in <b>Building Fund 2026</b>.\n\n")
    assert ("📂 Collection", f"pj:{pid}:col") in harness.buttons(q.edits[-1]["reply_markup"]) and len(bot.texts(GROUP.id)) == notices
    assert q.edits[0] == {"text": "⏳ Moving <b>God I'm Just Thankful</b> and its Google Drive folder…", "reply_markup": None}

    # 3. a project name must stay unique where it arrives
    q = await harness.press(LEAD, f"pj:{top_opening}:setcol:{bf_id}")
    assert q.edits[-1]["text"].startswith("❌ A project named “Opening” already exists in “Building Fund 2026”. Rename one of them first.")
    with session_scope() as s:
        p = project_service.get_project(s, top_opening)
        assert p.collection_id is None and drive.path_of(p.drive_root_id) == "Archive Root/Opening"

    # 4. into a collection that does not exist yet
    q = await harness.press(LEAD, f"pj:{pid}:setcol:new")
    assert "Send the <b>collection name</b>" in bot.last(LEAD.id)["text"]
    await harness.text(LEAD, "x")
    assert "Collection name must be between 2 and 100" in bot.last(LEAD.id)["text"]
    await harness.text(LEAD, "building FUND 2026")  # the collection it is in, spelled differently
    assert "is already in Building Fund 2026" in _plain(bot.last(LEAD.id)["text"]) and "Send another collection name" in bot.last(LEAD.id)["text"]
    await harness.text(LEAD, "  Thanksgiving   2026 ")
    text = bot.last(LEAD.id)["text"]
    assert "is now in <b>Thanksgiving 2026</b>" in text and "Thanksgiving 2026 / God I'm Just Thankful" in text
    assert ("📂 Collection", f"pj:{pid}:col") in harness.buttons(bot.last_markup(LEAD.id))
    assert drive.path_of(root_id) == "Archive Root/Thanksgiving 2026/God I'm Just Thankful"
    assert "moved from <b>Building Fund 2026</b> to the collection <b>Thanksgiving 2026</b>" in bot.last(GROUP.id)["text"]
    with session_scope() as s:
        assert [c.name for c in collection_service.list_collections(s)] == ["Building Fund 2026", "Thanksgiving 2026"]
        thanks_id = collection_service.find_by_name(s, "thanksgiving 2026").id
    await harness.text(LEAD, "Another Collection")  # the prompt is closed: plain text is a search again
    assert drive.path_of(root_id) == "Archive Root/Thanksgiving 2026/God I'm Just Thankful"
    q = await harness.press(LEAD, f"pj:{pid}:col")
    assert [t for t, _ in harness.buttons(q.edits[-1]["reply_markup"])][:2] == ["📁 Top level (take it out of the collection)", "📂 Building Fund 2026"]

    # 5. Google Drive refuses: nothing changes
    real_move = drive.move

    def refuse(*args, **kwargs):
        raise DriveError("Google Drive error 403 (permission denied or quota exceeded)")

    drive.move = refuse  # type: ignore[method-assign]
    group_messages = len(bot.texts(GROUP.id))
    q = await harness.press(LEAD, f"pj:{pid}:setcol:{bf_id}")
    assert q.edits[-1]["text"].startswith(
        "❌ Google Drive did not confirm the move: Google Drive error 403 (permission denied or quota exceeded). "
        "Nothing was changed in the bot; you can try again."
    )
    assert ("📂 Building Fund 2026", f"pj:{pid}:setcol:{bf_id}") in harness.buttons(q.edits[-1]["reply_markup"])
    drive.move = real_move  # type: ignore[method-assign]
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert (p.collection_id, p.collection) == (thanks_id, "Thanksgiving 2026")
    assert drive.path_of(root_id) == "Archive Root/Thanksgiving 2026/God I'm Just Thankful" and len(bot.texts(GROUP.id)) == group_messages

    # 6. junk button data, a collection that is gone, a cancelled project
    for data in ("setcol:abc", "setcol:", "setcol", "setcol:1e3", "setcol:" + "9" * 40):
        q = await harness.press(LEAD, f"pj:{pid}:{data}")
        assert q.answers == [("Invalid request.", True)] and q.edits == [], data
    for data in ("setcol:99999", "setcol:-1", "setcol:0"):
        q = await harness.press(LEAD, f"pj:{pid}:{data}")
        assert q.edits[-1]["text"].startswith("❌ That collection no longer exists."), data
    assert drive.path_of(root_id) == "Archive Root/Thanksgiving 2026/God I'm Just Thankful"
    await harness.press(LEAD, f"pj:{pid}:revoke")
    await harness.press(LEAD, f"pj:{pid}:revoke2")
    for data in ("col", "setcol:none", f"setcol:{bf_id}", "setcol:new"):
        q = await harness.press(LEAD, f"pj:{pid}:{data}")
        assert q.answers[-1] == ("Restore the project before moving it.", True), data
    with session_scope() as s:
        assert project_service.get_project(s, pid).collection_id == thanks_id
    # the Super Admin can move any project, an archived one included
    with session_scope() as s:
        p = project_service.get_project(s, top_opening)
        p.status = ProjectStatus.ARCHIVED
        s.commit()
    q = await harness.press(ADMIN, f"pj:{top_opening}:setcol:{thanks_id}")
    assert q.answers == [(None, False)] and q.edits[-1]["text"].startswith("✅ <b>Opening</b> is now in <b>Thanksgiving 2026</b>.")
    with session_scope() as s:
        p = project_service.get_project(s, top_opening)
        assert p.full_name == "Thanksgiving 2026 / Opening" and drive.path_of(p.drive_root_id) == "Archive Root/Thanksgiving 2026/Opening"
        assert p.status == ProjectStatus.ARCHIVED


@pytest.mark.asyncio
async def test_a_collection_never_shares_a_folder_with_a_project(harness, authorised_group, drive):
    """A collection named like a top-level project must not take that project's folder as its own."""
    from mg_archive_bot.services import collections as collection_service

    bot = harness.bot
    await run_wizard(harness, name="Easter 2026")
    await run_wizard(harness, name="Opening")
    with session_scope() as s:
        roots = {p.name: p.drive_root_id for p in project_service.list_projects(s)}
        ids = {p.name: p.id for p in project_service.list_projects(s)}

    # another project has that name: refused, and no collection is left behind
    await harness.press(LEAD, f"pj:{ids['Opening']}:setcol:new")
    await harness.text(LEAD, "Easter 2026")
    assert "A project at the top level is already called “Easter 2026”" in bot.last(LEAD.id)["text"]
    assert "Send another collection name or /cancel." in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        assert collection_service.list_collections(s) == []
        assert project_service.get_project(s, ids["Opening"]).collection_id is None
    # the wizard says so as soon as the name is sent, and creates nothing
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:new")
    await harness.text(LEAD, "easter 2026")
    assert "A project at the top level is already called “easter 2026”" in bot.last(LEAD.id)["text"]
    assert "Send another collection name or /cancel." in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        assert collection_service.list_collections(s) == []
    await harness.command(LEAD, "/cancel")
    # upper or lower case makes no difference, although Google Drive would treat the folder names as different
    await harness.press(LEAD, f"pj:{ids['Opening']}:setcol:new")
    await harness.text(LEAD, "EASTER 2026")
    assert "A project at the top level is already called “EASTER 2026”" in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        assert collection_service.list_collections(s) == []
    assert sorted(f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder) == ["Easter 2026", "Opening"]
    assert drive.path_of(roots["Opening"]) == "Archive Root/Opening" and drive.path_of(roots["Easter 2026"]) == "Archive Root/Easter 2026"
    await harness.text(LEAD, "Series")  # the prompt is still open: another name works
    assert drive.path_of(roots["Opening"]) == "Archive Root/Series/Opening"

    # the project's own name: the project becomes the first member of a collection of that name
    await harness.press(LEAD, f"pj:{ids['Easter 2026']}:setcol:new")
    await harness.text(LEAD, "easter 2026")
    assert "is now in <b>easter 2026</b>" in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        collection = collection_service.find_by_name(s, "Easter 2026")
        assert collection.drive_id != roots["Easter 2026"] and drive.path_of(collection.drive_id) == "Archive Root/easter 2026"
        assert project_service.get_project(s, ids["Easter 2026"]).full_name == "easter 2026 / Easter 2026"
        collection_id = collection.id
    assert drive.path_of(roots["Easter 2026"]) == "Archive Root/easter 2026/Easter 2026"
    assert sorted(f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder) == ["Series", "easter 2026"]

    # taking it out again puts a folder "Easter 2026" next to the collection folder; the collection keeps its own
    q = await harness.press(LEAD, f"pj:{ids['Easter 2026']}:setcol:none")
    assert q.edits[-1]["text"].startswith("✅")
    assert drive.path_of(roots["Easter 2026"]) == "Archive Root/Easter 2026"
    q = await harness.press(LEAD, f"pj:{ids['Opening']}:setcol:{collection_id}")
    assert q.edits[-1]["text"].startswith("✅") and drive.path_of(roots["Opening"]) == "Archive Root/easter 2026/Opening"

    # the wizard follows the same rule
    await harness.command(LEAD, "/newproject")
    await harness.press(LEAD, "nw:col:new")
    await harness.text(LEAD, "Easter 2026 ")  # the existing collection, whatever the spelling
    await harness.text(LEAD, "Worship")
    await harness.press(LEAD, "nw:decl:done")
    await harness.press(LEAD, f"nw:grp:{GROUP.id}")
    await harness.press(LEAD, "nw:meta:skip")
    await harness.press(LEAD, "nw:asg:done")
    q = await harness.press(LEAD, "nw:confirm")
    assert "Archive created" in q.edits[-1]["text"]
    with session_scope() as s:
        worship = [p for p in project_service.list_projects(s) if p.name == "Worship"][0]
        assert drive.path_of(worship.drive_root_id) == "Archive Root/easter 2026/Worship"

    # a record without members that points at a project's folder (written by an older version) gets its own folder
    with session_scope() as s:
        bad = collection_service.create_collection(s, "Broken", LEAD.id)
        bad.drive_id = roots["Easter 2026"]
        s.commit()
        bad_id = bad.id
    q = await harness.press(LEAD, f"pj:{ids['Opening']}:setcol:{bad_id}")
    assert q.edits[-1]["text"].startswith("✅ <b>Opening</b> is now in <b>Broken</b>.")
    with session_scope() as s:
        repaired = collection_service.get_collection(s, bad_id)
        assert repaired.drive_id != roots["Easter 2026"] and drive.path_of(repaired.drive_id) == "Archive Root/Broken"
    assert drive.path_of(roots["Opening"]) == "Archive Root/Broken/Opening"
    assert [f.name for f in drive.list_children(roots["Easter 2026"])] == ["Working File", "Final Render", "_Previews"]


@pytest.mark.asyncio
async def test_move_checks_the_collection_folder_first(harness, authorised_group, drive):
    from mg_archive_bot.services import collections as collection_service

    await run_wizard(harness, name="Song", collection="Building Fund 2026")
    await run_wizard(harness, name="Opening", collection="Building Fund 2026")
    with session_scope() as s:
        projects = {p.name: p for p in project_service.list_projects(s)}
        song, song_root, opening_root = projects["Song"].id, projects["Song"].drive_root_id, projects["Opening"].drive_root_id
        bf = projects["Song"].collection_id
        bf_folder = collection_service.get_collection(s, bf).drive_id
    q = await harness.press(LEAD, f"pj:{song}:setcol:none")
    assert q.edits[-1]["text"].startswith("✅")

    # the folder is in the trash while a project still lives in it: refused, nothing moves
    drive.delete(bf_folder)
    q = await harness.press(LEAD, f"pj:{song}:setcol:{bf}")
    assert q.edits[-1]["text"].startswith(
        "❌ The Google Drive folder of the collection “Building Fund 2026” is in the trash, and 1 project(s) still belong to it. "
        "Put the folder back in Google Drive first."
    )
    assert drive.path_of(song_root) == "Archive Root/Song" and not [c for c in drive.calls if c[0] == "move" and c[1][1] == bf_folder]
    drive.restore(bf_folder)
    q = await harness.press(LEAD, f"pj:{song}:setcol:{bf}")
    assert q.edits[-1]["text"].startswith("✅") and drive.path_of(song_root) == "Archive Root/Building Fund 2026/Song"

    # an emptied collection whose folder was thrown away gets a new folder when it is used again
    opening = projects["Opening"].id
    for pid in (song, opening):
        q = await harness.press(LEAD, f"pj:{pid}:setcol:none")
        assert q.edits[-1]["text"].startswith("✅")
    drive.delete(bf_folder)
    q = await harness.press(LEAD, f"pj:{song}:setcol:{bf}")
    assert q.edits[-1]["text"].startswith("✅ <b>Song</b> is now in <b>Building Fund 2026</b>.")
    with session_scope() as s:
        new_folder = collection_service.get_collection(s, bf).drive_id
    assert new_folder != bf_folder and drive.path_of(song_root) == "Archive Root/Building Fund 2026/Song"
    assert drive.get_file(song_root).trashed is False and drive.get_file(song_root).parents == (new_folder,)
    assert drive.path_of(opening_root) == "Archive Root/Opening"
    # the same when the folder has disappeared altogether
    await harness.press(LEAD, f"pj:{song}:setcol:none")
    del drive._nodes[new_folder]
    q = await harness.press(LEAD, f"pj:{opening}:setcol:{bf}")
    assert q.edits[-1]["text"].startswith("✅") and drive.path_of(opening_root) == "Archive Root/Building Fund 2026/Opening"


@pytest.mark.asyncio
async def test_names_stay_unique_when_projects_move_at_the_same_moment(harness, authorised_group, drive):
    import asyncio

    from mg_archive_bot.services import collections as collection_service

    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        target = collection_service.create_collection(s, "Target", LEAD.id)
        s.commit()
        target_id = target.id
    await run_wizard(harness, name="Opening")
    await run_wizard(harness, name="Opening", collection="Easter")
    await run_wizard(harness, name="Alpha")
    await run_wizard(harness, name="Beta")
    with session_scope() as s:
        by_name = {p.full_name: p.id for p in project_service.list_projects(s)}
    real_move, real_list = drive.move, drive.list_children

    def slow_move(*args, **kwargs):
        import time

        time.sleep(0.05)
        return real_move(*args, **kwargs)

    def slow_list(*args, **kwargs):
        import time

        time.sleep(0.02)
        return real_list(*args, **kwargs)

    drive.move, drive.list_children = slow_move, slow_list  # type: ignore[method-assign]
    # two projects with the same name head for the same collection: exactly one gets in
    first, second = await asyncio.gather(
        harness.press(LEAD, f"pj:{by_name['Opening']}:setcol:{target_id}"),
        harness.press(ADMIN, f"pj:{by_name['Easter / Opening']}:setcol:{target_id}"),
    )
    outcomes = sorted(q.edits[-1]["text"][:1] for q in (first, second))
    assert outcomes == ["✅", "❌"]
    refused = [q for q in (first, second) if q.edits[-1]["text"].startswith("❌")][0]
    assert "already exists in “Target”" in refused.edits[-1]["text"]
    with session_scope() as s:
        in_target = [p for p in project_service.list_projects(s) if p.collection_id == target_id]
        assert [p.name for p in in_target] == ["Opening"]
        folder = collection_service.get_collection(s, target_id).drive_id
    assert [f.name for f in drive.list_children(folder)] == ["Opening"]

    # two different projects head for a collection that has no folder yet: one folder, both inside
    with session_scope() as s:
        fresh = collection_service.create_collection(s, "Fresh", LEAD.id)
        s.commit()
        fresh_id = fresh.id
    first, second = await asyncio.gather(
        harness.press(LEAD, f"pj:{by_name['Alpha']}:setcol:{fresh_id}"),
        harness.press(ADMIN, f"pj:{by_name['Beta']}:setcol:{fresh_id}"),
    )
    assert first.edits[-1]["text"].startswith("✅") and second.edits[-1]["text"].startswith("✅")
    drive.move, drive.list_children = real_move, real_list  # type: ignore[method-assign]
    assert [f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder].count("Fresh") == 1
    with session_scope() as s:
        folder = collection_service.get_collection(s, fresh_id).drive_id
    assert sorted(f.name for f in drive.list_children(folder)) == ["Alpha", "Beta"]

    # a name taken while a draft was waiting in the wizard is noticed when the archive is created
    await harness.command(other, "/newproject")
    await harness.press(other, f"nw:col:{fresh_id}")
    await harness.text(other, "Gamma")
    await harness.press(other, "nw:decl:done")
    await harness.press(other, f"nw:grp:{GROUP.id}")
    await harness.press(other, "nw:meta:skip")
    await harness.press(other, "nw:asg:done")
    await run_wizard(harness, name="Gamma")
    with session_scope() as s:
        gamma = [p for p in project_service.list_projects(s) if p.name == "Gamma"][0].id
    q = await harness.press(LEAD, f"pj:{gamma}:setcol:{fresh_id}")
    assert q.edits[-1]["text"].startswith("✅")
    q = await harness.press(other, "nw:confirm")
    assert "A project named “Gamma” now exists in “Fresh”" in q.edits[-1]["text"]
    with session_scope() as s:
        created = [p for p in project_service.list_projects(s) if p.collection_id == fresh_id and p.status != ProjectStatus.DRAFT]
        assert sorted(p.name for p in created) == ["Alpha", "Beta", "Gamma"]
    assert sorted(f.name for f in drive.list_children(folder)) == ["Alpha", "Beta", "Gamma"]


@pytest.mark.asyncio
async def test_move_prompt_and_menu_details(harness, authorised_group, drive):
    from mg_archive_bot.bot import keyboards
    from mg_archive_bot.services import collections as collection_service
    from mg_archive_bot.services.drive import DriveError

    bot = harness.bot
    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
    await run_wizard(harness, name="Song")
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        pid, root_id = p.id, p.drive_root_id

    def collections() -> list[str]:
        with session_scope() as s:
            return [c.name for c in collection_service.list_collections(s) if c.name != "Waiting Room"]

    def folders_at_root() -> list[str]:
        return sorted(f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder)

    # the button is acknowledged before Google Drive is asked to do anything (its answer may come too late otherwise)
    from tests.fakes import FakeCallbackQuery

    events: list[str] = []
    real_answer, real_move = FakeCallbackQuery.answer, drive.move

    async def answer(self, *args, **kwargs):
        events.append("answer")
        return await real_answer(self, *args, **kwargs)

    def watched_move(*args, **kwargs):
        events.append("move")
        return real_move(*args, **kwargs)

    with session_scope() as s:
        waiting = collection_service.create_collection(s, "Waiting Room", LEAD.id)
        s.commit()
        waiting_id = waiting.id
    FakeCallbackQuery.answer, drive.move = answer, watched_move  # type: ignore[method-assign]
    try:
        await harness.press(LEAD, f"pj:{pid}:setcol:{waiting_id}")
        await harness.press(LEAD, f"pj:{pid}:setcol:none")
    finally:
        FakeCallbackQuery.answer, drive.move = real_answer, real_move  # type: ignore[method-assign]
    assert events == ["answer", "move", "answer", "move"] and drive.path_of(root_id) == "Archive Root/Song"
    assert folders_at_root() == ["Song", "Waiting Room"]

    # Google Drive fails while the typed collection is being set up: nothing is left behind, the name can be sent again
    real_move = drive.move

    def refuse(*args, **kwargs):
        raise DriveError("Google Drive error 500")

    drive.move = refuse  # type: ignore[method-assign]
    await harness.press(LEAD, f"pj:{pid}:setcol:new")
    await harness.text(LEAD, "Typo Colection")
    assert "did not confirm the move" in bot.last(LEAD.id)["text"] and "Send the name again to retry, or /cancel." in bot.last(LEAD.id)["text"]
    assert collections() == [] and drive.path_of(root_id) == "Archive Root/Song"
    assert folders_at_root() == ["Song", "Waiting Room"]  # neither a record nor a folder of the typo remains
    assert harness.user_data[LEAD.id]["prompt"]["kind"] == "move_collection"
    drive.move = real_move  # type: ignore[method-assign]

    # the lead changes while the prompt is open: the old lead can no longer move the project
    with session_scope() as s:
        project_service.set_lead(s, project_service.get_project(s, pid), other.id)
        s.commit()
    await harness.text(LEAD, "Easter")
    assert bot.last(LEAD.id)["text"] == "Only this project's lead (or the Super Admin) can do that."
    assert collections() == [] and drive.path_of(root_id) == "Archive Root/Song"
    assert "prompt" not in harness.user_data[LEAD.id]
    await harness.text(LEAD, "Easter")  # the prompt was closed: this is an ordinary search now
    assert collections() == []
    # ... nor can somebody who is no Team Lead any more
    await harness.press(other, f"pj:{pid}:setcol:new")
    with session_scope() as s:
        user_service.set_role(s, other.id, Role.DESIGNER, SUPER_ADMIN_ID)
    heard = len(bot.texts(other.id))
    await harness.text(other, "Easter")
    assert collections() == [] and drive.path_of(root_id) == "Archive Root/Song"
    assert len(bot.texts(other.id)) == heard and "prompt" not in harness.user_data[other.id]  # dropped without a word
    with session_scope() as s:
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        project_service.set_lead(s, project_service.get_project(s, pid), LEAD.id)
        s.commit()

    # the project is cancelled while the prompt is open: the prompt closes and no collection is created
    await harness.press(LEAD, f"pj:{pid}:setcol:new")
    await harness.press(ADMIN, f"pj:{pid}:revoke")
    await harness.press(ADMIN, f"pj:{pid}:revoke2")
    await harness.text(LEAD, "Easter 2027")
    assert bot.last(LEAD.id)["text"] == "This project was cancelled in the meantime. Restore it before moving it."
    assert "prompt" not in harness.user_data[LEAD.id]
    await harness.text(LEAD, "Another Try")
    assert collections() == [] and folders_at_root() == ["Waiting Room"]  # the project's folder is in the trash
    await harness.press(ADMIN, f"pj:{pid}:restore")

    # names with special characters are shown as typed, in private and in the group
    await harness.press(LEAD, f"pj:{pid}:setcol:new")
    await harness.text(LEAD, 'Q&A "Live"')
    assert "is now in <b>Q&amp;A \"Live\"</b>" in bot.last(LEAD.id)["text"]
    assert "is now part of the collection <b>Q&amp;A \"Live\"</b>" in bot.last(GROUP.id)["text"]
    assert drive.path_of(root_id) == 'Archive Root/Q&A "Live"/Song'

    # a label the lead typed for a top-level project is replaced by the collection's name: the menu says so
    await harness.press(LEAD, f"pj:{pid}:setcol:none")
    q = await harness.press(LEAD, f"pj:{pid}:col")
    assert "will be replaced" not in q.edits[-1]["text"]
    await harness.press(LEAD, f"pj:{pid}:meta")
    await harness.press(LEAD, f"pj:{pid}:mf:collection")
    await harness.text(LEAD, "Christmas Series")
    q = await harness.press(LEAD, f"pj:{pid}:col")
    assert "<i>Its Collection label “Christmas Series” will be replaced by the name of the collection.</i>" in q.edits[-1]["text"]

    # more collections than buttons: the menu says how to reach the others
    with session_scope() as s:
        for n in range(keyboards.MAX_COLLECTION_BUTTONS + 5):
            collection_service.create_collection(s, f"Series {n:02d}", LEAD.id)
        s.commit()
    q = await harness.press(LEAD, f"pj:{pid}:col")
    total = keyboards.MAX_COLLECTION_BUTTONS + 7  # the Series, Q&A "Live" and the Waiting Room
    assert f"Showing {keyboards.MAX_COLLECTION_BUTTONS} of {total} collections." in q.edits[-1]["text"]
    offered = [d for _, d in harness.buttons(q.edits[-1]["reply_markup"]) if ":setcol:" in d]
    assert len(offered) == keyboards.MAX_COLLECTION_BUTTONS + 1 and offered[-1] == f"pj:{pid}:setcol:new"
    await harness.press(LEAD, f"pj:{pid}:setcol:new")
    await harness.text(LEAD, f"series {keyboards.MAX_COLLECTION_BUTTONS + 4:02d}")  # one that has no button
    assert drive.path_of(root_id) == f"Archive Root/Series {keyboards.MAX_COLLECTION_BUTTONS + 4:02d}/Song"
    assert len(collections()) == total - 1  # nothing new: the helper leaves the Waiting Room out


@pytest.mark.asyncio
async def test_file_listing_follows_a_move_and_a_rename(harness, authorised_group, drive):
    bot = harness.bot
    await run_wizard(harness, name="Song", collection="Building Fund 2026")
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    await harness.press(LEAD, f"pj:{pid}:files")
    assert "📂 <b>Building Fund 2026 / Song</b>" in bot.last(LEAD.id)["text"]
    await harness.press(LEAD, f"pj:{pid}:setcol:none")
    await harness.press(ADMIN, f"pj:{pid}:files")
    assert "📂 <b>Song</b>" in bot.last(ADMIN.id)["text"] and "Building Fund" not in bot.last(ADMIN.id)["text"]
    await harness.press(LEAD, f"pj:{pid}:rename")
    await harness.text(LEAD, "Song of Thanks")
    await harness.press(ADMIN, f"pj:{pid}:files")
    assert "📂 <b>Song of Thanks</b>" in bot.last(ADMIN.id)["text"]


@pytest.mark.asyncio
async def test_moves_renames_and_new_archives_wait_for_each_other(harness, authorised_group, drive):
    """Whatever decides a project's name or place runs one at a time, and says so when it has to wait."""
    import asyncio

    from mg_archive_bot.bot.access import placement_lock
    from mg_archive_bot.services import collections as collection_service

    bot = harness.bot
    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
    await run_wizard(harness, name="Opening")
    await run_wizard(harness, name="Intro", collection="BF")
    await run_wizard(harness, name="Gamma")
    with session_scope() as s:
        ids = {p.full_name: p.id for p in project_service.list_projects(s)}
        bf = collection_service.find_by_name(s, "BF").id
        bf_folder = collection_service.find_by_name(s, "BF").drive_id
    real_move, real_rename, real_create = drive.move, drive.rename, drive.create_folder

    def slowly(real):
        def call(*args, **kwargs):
            import time

            time.sleep(0.03)
            return real(*args, **kwargs)

        return call

    drive.move, drive.rename, drive.create_folder = slowly(real_move), slowly(real_rename), slowly(real_create)  # type: ignore[method-assign]

    def in_bf() -> list[str]:
        with session_scope() as s:
            return sorted(p.name for p in project_service.list_projects(s) if p.collection_id == bf and p.status != ProjectStatus.DRAFT)

    # a rename to "Opening" inside BF and the move of the top-level "Opening" into BF: one of them is refused
    await harness.press(LEAD, f"pj:{ids['BF / Intro']}:rename")
    renamed, moved = await asyncio.gather(harness.text(LEAD, "Opening"), harness.press(ADMIN, f"pj:{ids['Opening']}:setcol:{bf}"))
    rename_ok = "Renamed" in bot.last(LEAD.id)["text"]
    move_ok = moved.edits[-1]["text"].startswith("✅")
    assert rename_ok != move_ok and in_bf().count("Opening") == 1 and len(in_bf()) == (2 if move_ok else 1)
    assert sorted(f.name for f in drive.list_children(bf_folder)).count("Opening") == 1
    await harness.command(LEAD, "/cancel")

    # a new archive "BF / Gamma" is created while the existing "Gamma" is moved into BF: one of them is refused
    await harness.command(other, "/newproject")
    await harness.press(other, f"nw:col:{bf}")
    await harness.text(other, "Gamma")
    await harness.press(other, "nw:decl:done")
    await harness.press(other, f"nw:grp:{GROUP.id}")
    await harness.press(other, "nw:meta:skip")
    await harness.press(other, "nw:asg:done")
    created, moved = await asyncio.gather(harness.press(other, "nw:confirm"), harness.press(LEAD, f"pj:{ids['Gamma']}:setcol:{bf}"))
    create_ok = "Archive created" in created.edits[-1]["text"]
    move_ok = moved.edits[-1]["text"].startswith("✅")
    assert create_ok != move_ok and in_bf().count("Gamma") == 1
    assert sorted(f.name for f in drive.list_children(bf_folder)).count("Gamma") == 1
    drive.move, drive.rename, drive.create_folder = real_move, real_rename, real_create  # type: ignore[method-assign]

    # somebody else's change is in progress: the lead is told, and a /cancel sent meanwhile is respected
    with session_scope() as s:
        song_id = [p.id for p in project_service.list_projects(s) if p.name == "Intro" or p.name == "Opening"][0]
        before = project_service.get_project(s, song_id).full_name
    ctx = harness.ctx(LEAD)
    await harness.press(LEAD, f"pj:{song_id}:setcol:new")
    await placement_lock(ctx).acquire()
    try:
        waiting = asyncio.create_task(harness.text(LEAD, "Series"))
        await asyncio.sleep(0.05)
        assert bot.last(LEAD.id)["text"].startswith("⏳ Another change to the archive is in progress.")
        await harness.command(LEAD, "/cancel")
    finally:
        placement_lock(ctx).release()
    await waiting
    assert "was not moved to “Series”: that step was closed before its turn came." in bot.last(LEAD.id)["text"]  # never silent
    with session_scope() as s:
        assert project_service.get_project(s, song_id).full_name == before
        assert collection_service.find_by_name(s, "Series") is None
    assert "Series" not in [f.name for f in drive.list_children(drive.ROOT_ID)]
    # a typo that was cancelled stays cancelled when the same step is opened again and the right name is sent
    await harness.press(LEAD, f"pj:{song_id}:setcol:new")
    await placement_lock(ctx).acquire()
    try:
        typo = asyncio.create_task(harness.text(LEAD, "Eatser 2026"))
        await asyncio.sleep(0.05)
        await harness.command(LEAD, "/cancel")
        await harness.press(LEAD, f"pj:{song_id}:setcol:new")
        right = asyncio.create_task(harness.text(LEAD, "Easter 2026"))
        await asyncio.sleep(0.05)
    finally:
        placement_lock(ctx).release()
    await asyncio.gather(typo, right)
    with session_scope() as s:
        assert project_service.get_project(s, song_id).collection == "Easter 2026"
        assert collection_service.find_by_name(s, "Eatser 2026") is None and collection_service.find_by_name(s, "Easter 2026") is not None
    assert "Eatser 2026" not in [f.name for f in drive.list_children(drive.ROOT_ID)]
    assert any("was not moved to “Eatser 2026”" in t for t in bot.texts(LEAD.id))
    assert "prompt" not in harness.user_data[LEAD.id]  # the step that was carried out is closed
    # a step the lead opened in the meantime is not closed by the one that finishes
    await harness.press(LEAD, f"pj:{song_id}:setcol:new")
    await placement_lock(ctx).acquire()
    try:
        first = asyncio.create_task(harness.text(LEAD, "Pentecost"))
        await asyncio.sleep(0.05)
        await harness.press(LEAD, f"pj:{song_id}:rename")  # another step, another prompt
    finally:
        placement_lock(ctx).release()
    await first
    assert "was not moved to “Pentecost”" in bot.last(LEAD.id)["text"]
    assert harness.user_data[LEAD.id]["prompt"]["kind"] == "rename"
    await harness.command(LEAD, "/cancel")
    # ... also when that other step is opened while Google Drive is already moving the folder
    real = drive.move

    def slow(*args, **kwargs):
        import time

        time.sleep(0.15)
        return real(*args, **kwargs)

    drive.move = slow  # type: ignore[method-assign]
    await harness.press(LEAD, f"pj:{song_id}:setcol:new")
    moving = asyncio.create_task(harness.text(LEAD, "Pentecost"))
    await asyncio.sleep(0.05)
    await harness.press(LEAD, f"pj:{song_id}:rename")
    await moving
    drive.move = real  # type: ignore[method-assign]
    with session_scope() as s:
        assert project_service.get_project(s, song_id).collection == "Pentecost"
    assert harness.user_data[LEAD.id]["prompt"]["kind"] == "rename"
    await harness.command(LEAD, "/cancel")
    with session_scope() as s:
        before = project_service.get_project(s, song_id).full_name
    # the same for a rename
    await harness.press(LEAD, f"pj:{song_id}:rename")
    await placement_lock(ctx).acquire()
    try:
        waiting = asyncio.create_task(harness.text(LEAD, "Never Mind"))
        await asyncio.sleep(0.05)
        assert bot.last(LEAD.id)["text"].startswith("⏳ Another change to the archive is in progress.")
        await harness.command(LEAD, "/cancel")
    finally:
        placement_lock(ctx).release()
    await waiting
    assert "was not renamed to “Never Mind”: that step was closed before its turn came." in bot.last(LEAD.id)["text"]
    with session_scope() as s:
        assert project_service.get_project(s, song_id).full_name == before
    # an ordinary rename is carried out and closes its step
    await harness.press(LEAD, f"pj:{song_id}:rename")
    await harness.text(LEAD, "Renamed Fine")
    assert "Renamed" in bot.last(LEAD.id)["text"] and "prompt" not in harness.user_data[LEAD.id]


@pytest.mark.asyncio
async def test_collection_names_in_other_alphabets_and_a_database_failure(harness, authorised_group, drive):
    from sqlalchemy.exc import OperationalError

    from mg_archive_bot.services import collections as collection_service

    bot = harness.bot
    await run_wizard(harness, name="Opening", collection="NOËL 2026")
    await run_wizard(harness, name="Song")
    with session_scope() as s:
        ids = {p.name: (p.id, p.drive_root_id) for p in project_service.list_projects(s)}
    pid, root_id = ids["Song"]
    # typing the name of an existing collection re-uses it, however it is spelled
    await harness.press(LEAD, f"pj:{pid}:setcol:new")
    await harness.text(LEAD, "noël 2026")
    assert "is now in <b>NOËL 2026</b>" in bot.last(LEAD.id)["text"]
    assert drive.path_of(root_id) == "Archive Root/NOËL 2026/Song"
    with session_scope() as s:
        assert [c.name for c in collection_service.list_collections(s)] == ["NOËL 2026"]
        assert project_service.get_project(s, pid).full_name == "NOËL 2026 / Song"
    assert sorted(f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder) == ["NOËL 2026"]
    await harness.press(LEAD, f"pj:{pid}:setcol:new")
    await harness.text(LEAD, "NOËL 2026")
    assert "is already in NOËL 2026" in _plain(bot.last(LEAD.id)["text"])
    await harness.command(LEAD, "/cancel")

    # the database fails after Google Drive has moved the folder: the lead is told the truth, and a repeat repairs it
    real_move_project = project_service.move_project

    async def moved_but_not_saved(session, project, target, drive_client, settings):
        await real_move_project(session, project, target, drive_client, settings)
        raise OperationalError("UPDATE projects", {}, Exception("database is locked"))

    project_service.move_project = moved_but_not_saved  # type: ignore[assignment]
    try:
        q = await harness.press(LEAD, f"pj:{pid}:setcol:none")
    finally:
        project_service.move_project = real_move_project  # type: ignore[assignment]
    assert q.edits[-1]["text"].startswith(
        "❌ The bot could not save the change. If the Google Drive folder was moved already, "
        "choosing the same collection again puts the record right."
    )
    assert harness.buttons(q.edits[-1]["reply_markup"])[0] == ("📁 Top level (take it out of the collection)", f"pj:{pid}:setcol:none")
    assert drive.path_of(root_id) == "Archive Root/Song"
    with session_scope() as s:
        assert project_service.get_project(s, pid).full_name == "NOËL 2026 / Song"  # the record is behind
    q = await harness.press(LEAD, f"pj:{pid}:setcol:none")
    assert q.edits[-1]["text"].startswith("✅") and drive.path_of(root_id) == "Archive Root/Song"
    with session_scope() as s:
        assert project_service.get_project(s, pid).full_name == "Song"
    assert len([c for c in drive.calls if c[0] == "move"]) == 3


@pytest.mark.asyncio
async def test_same_new_collection_name_sent_twice_while_drive_recovers(harness, authorised_group, drive):
    import asyncio

    from mg_archive_bot.services import collections as collection_service
    from mg_archive_bot.services.drive import DriveError

    bot = harness.bot
    await run_wizard(harness, name="Song")
    await run_wizard(harness, name="Hymn")
    with session_scope() as s:
        ids = {p.name: (p.id, p.drive_root_id) for p in project_service.list_projects(s)}
    real_move = drive.move
    attempts = {"n": 0}

    def first_fails(*args, **kwargs):
        import time

        attempts["n"] += 1
        time.sleep(0.05)
        if attempts["n"] == 1:
            raise DriveError("Google Drive error 503")
        return real_move(*args, **kwargs)

    drive.move = first_fails  # type: ignore[method-assign]
    await harness.press(LEAD, f"pj:{ids['Song'][0]}:setcol:new")
    await harness.press(ADMIN, f"pj:{ids['Hymn'][0]}:setcol:new")
    await asyncio.gather(harness.text(LEAD, "Easter 2027"), harness.text(ADMIN, "easter 2027"))
    drive.move = real_move  # type: ignore[method-assign]
    replies = sorted(t[:1] for t in (bot.last(LEAD.id)["text"], bot.last(ADMIN.id)["text"]))
    assert replies == ["✅", "❌"]
    with session_scope() as s:
        stored = collection_service.list_collections(s)
        assert len(stored) == 1 and stored[0].name.lower() == "easter 2027"
        members = [p for p in project_service.list_projects(s) if p.collection_id == stored[0].id]
        assert len(members) == 1
        moved_root, folder = members[0].drive_root_id, stored[0].drive_id
        left = [p for p in project_service.list_projects(s) if p.collection_id is None]
        assert len(left) == 1
    # Drive and the records agree, and there is one folder of that name
    assert drive.get_file(moved_root).parents == (folder,) and drive.get_file(left[0].drive_root_id).parents == (drive.ROOT_ID,)
    at_root = sorted(f.name for f in drive.list_children(drive.ROOT_ID) if f.is_folder)
    assert len(at_root) == 2 and sum(name.lower() == "easter 2027" for name in at_root) == 1
    # the one who was refused can simply send the name again
    loser = LEAD if bot.last(LEAD.id)["text"].startswith("❌") else ADMIN
    assert "Send the name again to retry, or /cancel." in bot.last(loser.id)["text"]
    await harness.text(loser, "Easter 2027")
    assert bot.last(loser.id)["text"].startswith("✅")
    with session_scope() as s:
        assert len(collection_service.list_collections(s)) == 1
        assert all(p.collection_id is not None for p in project_service.list_projects(s))


@pytest.mark.asyncio
async def test_file_listing_made_during_a_move_is_not_kept(harness, authorised_group, drive):
    import asyncio

    bot = harness.bot
    await run_wizard(harness, name="Song", collection="BF")
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
    real_move = drive.move

    def slow_move(*args, **kwargs):
        import time

        time.sleep(0.1)
        return real_move(*args, **kwargs)

    drive.move = slow_move  # type: ignore[method-assign]
    # the Super Admin asks for the files while the lead's move is in flight: that answer may carry the old name,
    # but whoever asks afterwards gets the new one
    await asyncio.gather(harness.press(LEAD, f"pj:{pid}:setcol:none"), harness.press(ADMIN, f"pj:{pid}:files"))
    drive.move = real_move  # type: ignore[method-assign]
    other = FakeUser(2002, "Olga", "Other", username="olga")
    with session_scope() as s:
        user_service.register_designer(s, other.id, other.full_name, other.username)
        user_service.set_role(s, other.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        assert project_service.get_project(s, pid).full_name == "Song"
    await harness.press(other, f"pj:{pid}:files")
    assert "📂 <b>Song</b>" in bot.last(other.id)["text"] and "BF" not in bot.last(other.id)["text"]


@pytest.mark.asyncio
async def test_double_tap_on_a_destination_moves_once(harness, authorised_group, drive):
    import asyncio

    from mg_archive_bot.services import collections as collection_service
    from tests.fakes import FakeChat, FakeMessage

    bot = harness.bot
    await run_wizard(harness, name="Song")
    with session_scope() as s:
        pid = project_service.list_projects(s)[0].id
        bf = collection_service.create_collection(s, "BF", LEAD.id)
        s.commit()
        bf_id = bf.id
    notices = len(bot.texts(GROUP.id))
    menu = FakeMessage(bot, FakeChat(LEAD.id, "private"), text="(menu)", from_user=LEAD)  # both taps hit the same message
    first, second = await asyncio.gather(
        harness.press(LEAD, f"pj:{pid}:setcol:{bf_id}", message=menu), harness.press(LEAD, f"pj:{pid}:setcol:{bf_id}", message=menu)
    )
    assert len(bot.texts(GROUP.id)) == notices + 1 and len([c for c in drive.calls if c[0] == "move"]) == 1
    finals = sorted(q.edits[-1]["text"][:1] for q in (first, second))
    assert finals == ["ℹ", "✅"]
    assert "❌" not in menu.text and ("📂 Collection", f"pj:{pid}:col") in harness.buttons(menu.reply_markup)
