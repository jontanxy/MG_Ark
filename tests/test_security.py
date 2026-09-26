"""Security regression tests: abuse cases every role can attempt, and the guards that must hold."""
from __future__ import annotations

import os
import stat

import pytest

from mg_archive_bot.constants import AssetCategory, ProjectStatus, Role
from mg_archive_bot.db import session_scope
from mg_archive_bot.models import PreviewAsset
from mg_archive_bot.services import groups as group_service
from mg_archive_bot.services import projects as project_service
from mg_archive_bot.services import users as user_service
from mg_archive_bot.util import clip_message, normalise_terms
from tests.conftest import PASSWORD, SUPER_ADMIN_ID
from tests.fakes import BotHarness, FakeChat, FakeMessage, FakeUpdate, FakeUser

ADMIN = FakeUser(SUPER_ADMIN_ID, "Boss", username="boss")
LEAD = FakeUser(2001, "Lee", "Lead", username="lead")
DESIGNER = FakeUser(3001, "Dee", "Signer", username="dee")
LIGHTS = FakeUser(6001, "Lucy", "Lights", username="lucy")
REVOKED = FakeUser(7001, "Rev", "Oked")
STRANGER = FakeUser(4001, "Ran", "Dom")
GROUP = FakeChat(-100500, "supergroup", "MG Team")
OTHER_GROUP = FakeChat(-100999, "supergroup", "Not authorised")


@pytest.fixture
def world(db, settings, drive):
    """A populated bot: one live project linked to an authorised group, every role registered."""
    h = BotHarness(settings, drive)
    with session_scope() as s:
        user_service.ensure_super_admin(s, ADMIN.id, ADMIN.full_name, ADMIN.username)
        for u in (LEAD, DESIGNER, LIGHTS, REVOKED):
            user_service.register_designer(s, u.id, u.full_name, u.username)
        user_service.set_role(s, LEAD.id, Role.TEAM_LEAD, SUPER_ADMIN_ID)
        user_service.set_role(s, LIGHTS.id, Role.LIGHTS, SUPER_ADMIN_ID)
        user_service.revoke_user(s, REVOKED.id, SUPER_ADMIN_ID)
        group_service.authorise_group(s, GROUP.id, GROUP.title, group_service.create_token(s, LEAD.id, 24))
        p = project_service.create_draft(s, "Live Song", LEAD.id, "Lee", 2026)
        project_service.set_declaration(s, p, AssetCategory.TIMELINE, True)
        project_service.set_metadata_field(s, p, "tags", "worship, gold")
        project_service.toggle_assignment(s, p, DESIGNER.id, AssetCategory.ALL)
    return h


def _snapshot():
    """Everything a privilege escalation could change, for before/after comparison."""
    with session_scope() as s:
        return {
            "users": sorted((u.telegram_id, u.role.value, u.status.value) for u in user_service.list_users(s)),
            "groups": sorted((g.chat_id, g.status.value) for g in group_service.list_groups(s)),
            "projects": sorted(
                (p.id, p.status.value, p.mg_group_chat_id, p.has_psd, tuple(sorted(a.user_id for a in p.assignments)), p.style)
                for p in project_service.list_projects(s)
            ),
        }


async def _project(h):
    with session_scope() as s:
        p = project_service.list_projects(s)[0]
        if p.status == ProjectStatus.DRAFT:
            await project_service.provision_folders(s, p, h.bot_data["drive"], h.bot_data["settings"])
            project_service.set_group(s, p, GROUP.id)
        return p.id


PRIVILEGED_CALLBACKS = [
    "pj:{pid}:menu", "pj:{pid}:check", "pj:{pid}:announce", "pj:{pid}:remind", "pj:{pid}:assign", "pj:{pid}:asgcat:ALL",
    "pj:{pid}:asg:ALL:{did}", "pj:{pid}:meta", "pj:{pid}:mf:style", "pj:{pid}:decl", "pj:{pid}:dt:PSD", "pj:{pid}:group",
    "pj:{pid}:grp:none", "pj:{pid}:prev", "pj:{pid}:previews", "pj:{pid}:verify", "pj:{pid}:verify2", "pj:{pid}:revoke",
    "pj:{pid}:revoke2", "pj:{pid}:restore", "pj:{pid}:reopen", "pj:{pid}:files", "pl:0:open",
    "nw:col:none", "nw:decl:done", "nw:asg:done", "nw:confirm", "nw:cancel",
    "ad:users", "ad:groups", "ad:u:{did}", "ad:u:{did}:role:TEAM_LEAD", "ad:u:{did}:revoke", "ad:u:{did}:restore",
    "ad:g:{gid}:revoke", "ad:g:{gid}:revoke2", "ad:g:{gid}:restore",
]
PRIVILEGED_COMMANDS = ["/newproject", "/projects", "/project 1", "/creategroup", "/sheet", "/users", "/groups", "/setpassword"]


@pytest.mark.asyncio
@pytest.mark.parametrize("attacker", [STRANGER, REVOKED, LIGHTS, DESIGNER], ids=["stranger", "revoked", "lights", "designer"])
async def test_lower_roles_cannot_reach_privileged_actions(world, attacker):
    """Every Team Lead / Super Admin action is refused for lower roles, even with hand-crafted button data."""
    h = world
    pid = await _project(h)
    before = _snapshot()
    for template in PRIVILEGED_CALLBACKS:
        data = template.format(pid=pid, did=DESIGNER.id, gid=GROUP.id)
        q = await h.press(attacker, data)
        assert q.answers and q.answers[-1][1] is True and q.edits == [], data  # an alert, no edits
        assert not any(m["chat_id"] == GROUP.id for m in h.bot.sent), data  # nothing posted to the group
    for cmd in PRIVILEGED_COMMANDS:
        await h.command(attacker, cmd)
        text = h.bot.last(attacker.id)["text"]
        assert "requires the" in text or "not registered" in text or "revoked" in text, (cmd, text)
    assert _snapshot() == before
    # pending text prompts never accept privileged input from lower roles either
    h.user_data.setdefault(attacker.id, {})
    from mg_archive_bot.bot.access import set_prompt

    for kind, payload in (("new_password", "hacked-password-1"), ("project_name", "Injected"), ("meta_value", "x")):
        set_prompt(h.ctx(attacker), kind, project_id=pid, field="style")
        await h.text(attacker, payload)
    assert _snapshot() == before
    with session_scope() as s:
        assert user_service.verify_access_password(s, PASSWORD)


@pytest.mark.asyncio
async def test_team_lead_cannot_reach_super_admin_actions(world):
    h = world
    before = _snapshot()
    for data in ("ad:users", "ad:groups", f"ad:u:{DESIGNER.id}:role:TEAM_LEAD", f"ad:u:{DESIGNER.id}:revoke", f"ad:g:{GROUP.id}:revoke2"):
        q = await h.press(LEAD, data)
        assert "Super Admin" in q.answers[-1][0] and q.edits == []
    for cmd in ("/users", "/groups", "/setpassword"):
        await h.command(LEAD, cmd)
        assert "requires the Super Admin role" in h.bot.last(LEAD.id)["text"]
    assert _snapshot() == before


@pytest.mark.asyncio
async def test_super_admin_cannot_be_demoted_or_revoked_and_role_cannot_be_minted(world):
    h = world
    for data in (f"ad:u:{ADMIN.id}:revoke", f"ad:u:{ADMIN.id}:role:DESIGNER", f"ad:u:{DESIGNER.id}:role:SUPER_ADMIN"):
        q = await h.press(ADMIN, data)
        assert q.answers[-1][1] is True and ("Super Admin" in q.answers[-1][0] or "Invalid" in q.answers[-1][0]), data
    with session_scope() as s:
        assert user_service.get_user(s, ADMIN.id).role == Role.SUPER_ADMIN
        assert user_service.get_user(s, DESIGNER.id).role == Role.DESIGNER
        assert [u.role for u in user_service.list_users(s)].count(Role.SUPER_ADMIN) == 1


@pytest.mark.asyncio
async def test_malformed_callback_data_never_raises_or_acts(world):
    h = world
    pid = await _project(h)
    huge = "9" * 5000  # would make int() raise on 3.11+
    cases = [
        "pj:1:", f"pj:{pid}:asg:BOGUS:1", f"pj:{pid}:asg:ALL:abc", f"pj:{pid}:asg", f"pj:{pid}:mf:", f"pj:{pid}:dt:BOGUS", f"pj:{pid}:dt",
        f"pj:{pid}:grp:abc", f"pj:{pid}:grp", f"pj:{pid}:asgcat:BOGUS", f"pj:{pid}:asgcat", f"pj:{huge}:menu", "pj:1:zzz",
        "ad:u:abc", "ad:u", "ad:u:99:role:BOGUS", "ad:u:99:role", "ad:g:abc:revoke", "ad:g", "ad:x", f"ad:u:{huge}",
        "nw:grp:abc", "nw:col:abc", f"nw:col:{huge}", "nw:asg:abc", "nw:decl:BOGUS", "nw:decl:ALL", "nw:zzz",
        f"sr:{pid}:zzz", f"sr:{huge}:prev", "sp:99999", "pl:0:zzz", "pl:999:open",
    ]
    before = _snapshot()
    for user in (ADMIN, LEAD):
        # give the wizard a live draft so wizard branches are exercised, not short-circuited
        await h.command(LEAD, "/newproject")
        await h.press(LEAD, "nw:col:none")
        await h.text(LEAD, "Draft For Fuzz")
        for data in cases:
            try:
                await h.press(user, data)
            except AssertionError:
                continue  # no handler registered for this prefix: PTB would simply ignore it
        await h.command(LEAD, "/cancel")
    assert _snapshot() == before


@pytest.mark.asyncio
async def test_draft_and_cancelled_projects_are_not_reachable_through_search_buttons(world):
    h = world
    with session_scope() as s:
        draft = project_service.create_draft(s, "Secret Draft", ADMIN.id, "Boss", 2026).id  # another lead's draft
        c = project_service.create_draft(s, "Cancelled One", LEAD.id, "Lee", 2026)
        await project_service.provision_folders(s, c, h.bot_data["drive"], h.bot_data["settings"])
        await project_service.revoke_project(s, c, h.bot_data["drive"], LEAD.id)
        cancelled = c.id
    for pid in (draft, cancelled):
        for action in ("details", "files", "prev"):
            n = len(h.bot.texts(DESIGNER.id))
            q = await h.press(DESIGNER, f"sr:{pid}:{action}")
            assert q.answers[-1] == ("Project not found.", True) and len(h.bot.texts(DESIGNER.id)) == n
    await h.command(DESIGNER, "/search secret, cancelled")
    assert "No projects match" in h.bot.last(DESIGNER.id)["text"]


@pytest.mark.asyncio
async def test_oversized_inputs_cannot_break_messages(world):
    h = world
    pid = await _project(h)
    limit = 4096
    await h.command(DESIGNER, "/search " + "a" * 10_000)
    assert len(h.bot.last(DESIGNER.id)["text"]) < limit
    await h.command(DESIGNER, "/search " + ", ".join(f"term{i}" for i in range(5_000)))
    assert len(h.bot.last(DESIGNER.id)["text"]) < limit
    assert len(normalise_terms(", ".join(f"t{i}" for i in range(5_000)))) == 50
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert len(project_service.set_tags(s, p, ", ".join(f"tag{i}" + "x" * 500 for i in range(2_000)))) == 20
        assert all(len(t) <= 40 for t in p.tag_names)
        for field in ("collection", "event", "ministry", "style", "colours", "creator", "asset_types"):
            project_service.set_metadata_field(s, p, field, "y" * 10_000)
        project_service.set_metadata_field(s, p, "description", "z" * 10_000)
        for i in range(60):
            user_service.register_designer(s, 10_000 + i, f"Designer Number {i} With A Long Name", None)
            project_service.toggle_assignment(s, p, 10_000 + i, AssetCategory.ALL)
    await h.command(DESIGNER, "/search live")
    assert all(len(t) <= limit for t in h.bot.texts(DESIGNER.id)[-2:])
    await h.press(DESIGNER, f"sr:{pid}:details")
    assert len(h.bot.last(DESIGNER.id)["text"]) <= limit
    q = await h.press(LEAD, f"pj:{pid}:menu")
    assert len(q.edits[-1]["text"]) <= limit
    q = await h.press(LEAD, f"pj:{pid}:check")
    assert len(q.edits[-1]["text"]) <= limit and all(len(m["text"]) <= limit for m in h.bot.sent if m["chat_id"] == GROUP.id)
    q = await h.press(LEAD, f"pj:{pid}:announce")
    assert all(len(m["text"]) <= limit for m in h.bot.sent if m["chat_id"] == GROUP.id)
    await h.command(LEAD, "/newproject")
    await h.press(LEAD, "nw:col:new")
    await h.text(LEAD, "c" * 5_000)
    assert "between 2 and 100" in h.bot.last(LEAD.id)["text"]
    await h.command(LEAD, "/cancel")


def test_clip_message_keeps_lines_whole():
    lines = [f'<a href="https://x/{i}">line {i}</a>' for i in range(400)]
    clipped = clip_message("\n".join(lines))
    assert len(clipped) <= 4000 and clipped.endswith("\n…")
    assert all(line.startswith("<a href") and line.endswith("</a>") for line in clipped.split("\n")[:-1])
    assert clip_message("short") == "short"


@pytest.mark.asyncio
async def test_html_in_user_controlled_text_is_always_escaped(world):
    h = world
    evil = FakeUser(8001, "<script>alert(1)</script>", "<b>x</b>", username="<i>u</i>")
    with session_scope() as s:
        user_service.register_designer(s, evil.id, evil.full_name, evil.username)
        p = project_service.list_projects(s)[0]
        project_service.set_metadata_field(s, p, "tags", "<b>injected</b>, ok&fine")
        project_service.set_metadata_field(s, p, "description", "<a href='x'>link</a> & more")
        project_service.set_metadata_field(s, p, "event", "Easter <3")
        project_service.toggle_assignment(s, p, evil.id, AssetCategory.ALL)
        with pytest.raises(project_service.ProjectError):
            project_service.create_draft(s, "Name <b>with tags</b>", LEAD.id, "Lee", 2026)
    pid = await _project(h)
    await h.command(evil, "/start")
    await h.command(evil, "/whoami")
    await h.command(DESIGNER, "/search ok&fine")
    await h.press(DESIGNER, f"sr:{pid}:details")
    await h.press(LEAD, f"pj:{pid}:check")
    await h.press(LEAD, f"pj:{pid}:announce")
    await h.command(ADMIN, "/users")
    await h.press(ADMIN, f"ad:u:{evil.id}")
    await h.chat_member(FakeChat(-100777, "supergroup", "<b>Evil group</b>"), evil, "left", "member")
    sent = [m["text"] for m in h.bot.sent] + [e["text"] for e in h.bot.edited]
    for text in sent:
        assert "<script>" not in text and "<b>x</b>" not in text and "<i>u</i>" not in text and "<b>injected</b>" not in text
        assert "<a href='x'>" not in text and "<b>Evil group</b>" not in text
    assert any("&lt;script&gt;" in t for t in sent) and any("&lt;b&gt;injected&lt;/b&gt;" in t for t in sent)


@pytest.mark.asyncio
async def test_group_access_needs_both_authorised_user_and_group(world):
    h = world
    for cmd in ("/status", "/files", "/remind"):
        h.bot_data.pop("limiters", None)
        await h.command(LEAD, cmd, chat=OTHER_GROUP)
        assert "not an authorised MG Group" in h.bot.last(OTHER_GROUP.id)["text"]
        n = len(h.bot.texts(GROUP.id))
        await h.command(STRANGER, cmd, chat=GROUP)
        await h.command(REVOKED, cmd, chat=GROUP)
        texts = h.bot.texts(GROUP.id)[n:]
        assert len(texts) == 2 and ("not an authorised user" in texts[0]) and ("revoked" in texts[1])
    # unknown senders are answered at most once per minute — no reply amplification
    h.bot_data.pop("limiters", None)
    n = len(h.bot.texts(GROUP.id))
    for _ in range(20):
        await h.command(STRANGER, "/status", chat=GROUP)
    assert len(h.bot.texts(GROUP.id)) == n + 1
    # an unauthorised group cannot be activated by a Designer, and token guessing is rate limited
    with session_scope() as s:
        token = group_service.create_token(s, LEAD.id, 24).token
    await h.command(DESIGNER, f"/activate {token}", chat=OTHER_GROUP)
    assert "requires the Team Lead role" in h.bot.last(OTHER_GROUP.id)["text"]
    n = len(h.bot.texts(OTHER_GROUP.id))
    for i in range(30):
        await h.command(LEAD, f"/activate MG-AAAA-{i:04d}", chat=OTHER_GROUP)
    assert len(h.bot.texts(OTHER_GROUP.id)) - n == 5  # then silence
    with session_scope() as s:
        assert not group_service.is_group_authorised(s, OTHER_GROUP.id)


@pytest.mark.asyncio
async def test_password_flow_resists_abuse(world, settings):
    h = world
    # wrong guesses lock the account; the password is never echoed; the message is deleted
    await h.command(STRANGER, "/start")
    for _ in range(5):
        u = await h.text(STRANGER, "guess")
        assert u.message.deleted
    assert "Locked" in h.bot.last(STRANGER.id)["text"] and "guess" not in " ".join(h.bot.texts(STRANGER.id))
    assert "Login lockout" in h.bot.last(ADMIN.id)["text"]
    # a locked account is refused even with the right password
    h.bot_data.pop("limiters", None)
    await h.command(STRANGER, "/start")
    assert "Too many failed attempts" in h.bot.last(STRANGER.id)["text"]
    with session_scope() as s:
        assert user_service.get_user(s, STRANGER.id) is None
    # many accounts guessing trips the global breaker
    settings.password_breaker_failures = 6
    h.bot_data.pop("password_breaker", None)  # breaker is built from settings on first use
    for i in range(6):
        bot = FakeUser(20_000 + i, f"Bot{i}")
        h.bot_data.pop("limiters", None)
        await h.command(bot, "/start")
        await h.text(bot, "guess")
    assert "Registration paused" in h.bot.last(ADMIN.id)["text"]
    late = FakeUser(30_000, "Late")
    h.bot_data.pop("limiters", None)
    await h.command(late, "/start")
    assert "temporarily paused" in h.bot.last(late.id)["text"]
    # the password itself never appears in any outgoing message
    assert PASSWORD not in " ".join(m["text"] for m in h.bot.sent)


@pytest.mark.asyncio
async def test_error_handler_never_leaks_internals(world):
    from telegram.error import BadRequest

    from mg_archive_bot.bot.handlers.common import error_handler

    h = world
    chat = FakeChat(DESIGNER.id, "private")
    for err in (BadRequest("Can't parse entities: unsupported start tag \"script\" at byte offset 12"), RuntimeError("secret internal path /Users/x/.env")):
        ctx = h.ctx(DESIGNER)
        ctx.error = err
        await error_handler(FakeUpdate(DESIGNER, chat, message=FakeMessage(h.bot, chat, text="x", from_user=DESIGNER)), ctx)
        text = h.bot.last(DESIGNER.id)["text"]
        assert text == "⚠️ Something went wrong. Please try again."


def test_bot_created_files_are_private(tmp_path, settings, monkeypatch):
    """Database, log and work directory end up readable only by the bot's user."""
    from mg_archive_bot import __main__ as entry

    db_path = tmp_path / "priv.sqlite3"
    log_path = tmp_path / "bot.log"
    work = tmp_path / "work"
    db_path.write_bytes(b"")
    log_path.write_text("")
    work.mkdir()
    os.chmod(db_path, 0o644)
    os.chmod(log_path, 0o644)
    os.chmod(work, 0o755)
    entry._restrict(db_path)
    entry._restrict(log_path)
    entry._restrict(work, 0o700)
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(work).st_mode) == 0o700
    entry._restrict(tmp_path / "missing")  # no-op, no error


@pytest.mark.asyncio
async def test_lights_never_see_drive_links_or_file_names(world):
    h = world
    pid = await _project(h)
    with session_scope() as s:
        s.add(PreviewAsset(project_id=pid, category=AssetCategory.TIMELINE, source_key="timeline_prores", source_drive_id="src", source_name="Loop.mov", preview_name="Loop.mp4", preview_drive_id="pv", preview_link="https://drive.google.com/file/d/pv/view", size_bytes=1, status=__import__("mg_archive_bot.constants", fromlist=["PreviewStatus"]).PreviewStatus.READY))
    await h.command(LIGHTS, "/search live")
    await h.press(LIGHTS, f"sr:{pid}:prev")
    await h.press(LIGHTS, f"sr:{pid}:details")
    await h.press(LIGHTS, f"sr:{pid}:files")
    await h.command(LIGHTS, "/files", chat=GROUP)
    await h.press(LIGHTS, f"pj:{pid}:files")
    texts = [m["text"] for m in h.bot.sent if m["chat_id"] in (LIGHTS.id, GROUP.id)]
    assert not any("drive.google.com/drive/folders" in t for t in texts), "folder links leaked to Lights"
    assert not any("📂" in t for t in texts), "file listing leaked to Lights"
    markups = [m["reply_markup"] for m in h.bot.sent if m["chat_id"] == LIGHTS.id and m["reply_markup"] is not None]
    assert all(all((b.url or "").startswith("https://drive.google.com/file/d/") or b.callback_data for row in mk.inline_keyboard for b in row) for mk in markups)


# ---------------------------------------------------------------------------------------------
# Regression tests for the findings of the manual review
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_users_get_no_dms_and_are_never_mentioned(world):
    from mg_archive_bot.bot.actions import notify_user
    from mg_archive_bot.services import notifications
    from mg_archive_bot.constants import build_folder_tree

    h = world
    pid = await _project(h)
    tree = build_folder_tree(h.bot_data["settings"].folder_names())
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert "Dee Signer" in notifications.announcement(p, tree)
        user_service.revoke_user(s, DESIGNER.id, SUPER_ADMIN_ID)
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        assert p.assignments == [] and "Dee Signer" not in notifications.announcement(p, tree)
        with pytest.raises(project_service.ProjectError):  # cannot be re-assigned while revoked
            project_service.toggle_assignment(s, p, DESIGNER.id, AssetCategory.ALL)
        # demotion to Lights also ends responsibilities
        user_service.register_designer(s, 9001, "Temp", None)
        project_service.toggle_assignment(s, p, 9001, AssetCategory.TIMELINE)
        user_service.set_role(s, 9001, Role.LIGHTS, SUPER_ADMIN_ID)
        assert p.assignments == []
    n = len(h.bot.texts(DESIGNER.id))
    await notify_user(h.ctx(LEAD), DESIGNER.id, "project secret")
    await notify_user(h.ctx(LEAD), 424242, "unknown user")
    assert len(h.bot.texts(DESIGNER.id)) == n and not h.bot.texts(424242)


@pytest.mark.asyncio
async def test_group_status_never_makes_the_caller_a_preview_requester(world, drive):
    import asyncio
    from types import SimpleNamespace

    h = world
    pid = await _project(h)
    captured = []

    async def enqueue(jobs):
        captured.extend(jobs)
        return len(jobs)

    h.bot_data["preview_worker"] = SimpleNamespace(enqueue=enqueue)
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        drive.put_file(p.folder("timeline_prores").drive_id, "Loop.mov", size=5_000_000, md5="a", mime_type="video/quicktime")
    await h.command(LIGHTS, "/status", chat=GROUP)
    await asyncio.sleep(0)
    assert captured and all(j.requested_by is None for j in captured)  # Lights never receive preview DMs


def test_preview_planning_respects_size_window_and_per_scan_cap(db, settings, drive):
    import asyncio

    from mg_archive_bot.services.previews import plan_previews
    from mg_archive_bot.services.validation import validate_project

    with session_scope() as s:
        p = project_service.create_draft(s, "Caps", 1, "Lead", 2026)
        project_service.set_declaration(s, p, AssetCategory.TIMELINE, True)
        asyncio.run(project_service.provision_folders(s, p, drive, settings))
        folder = p.folder("timeline_prores").drive_id
        drive.put_file(folder, "tiny.mov", size=10, md5="t", mime_type="video/quicktime")  # junk-sized
        drive.put_file(folder, "huge.mov", size=50 * 1024**3, md5="h", mime_type="video/quicktime")  # absurd
        for i in range(30):
            drive.put_file(folder, f"real{i:02d}.mov", size=5_000_000, md5=f"r{i}", mime_type="video/quicktime")
        result = asyncio.run(validate_project(s, p, drive, settings))
        jobs = plan_previews(s, p, result.source_files, min_size=1_000_000, max_size=20 * 1024**3, max_jobs=20)
        names = {j.source.name for j in jobs}
        assert len(jobs) == 20 and "tiny.mov" not in names and "huge.mov" not in names
        assert len(plan_previews(s, p, result.source_files, min_size=1_000_000, max_size=20 * 1024**3, max_jobs=20)) == 20  # still pending: re-queued, not doubled
        from mg_archive_bot.constants import PreviewStatus

        for row in p.previews:  # the worker finishes them...
            row.status = PreviewStatus.READY
        s.flush()
        remaining = plan_previews(s, p, result.source_files, min_size=1_000_000, max_size=20 * 1024**3, max_jobs=20)
        assert len(remaining) == 10  # ...and the next scan picks up the rest


@pytest.mark.asyncio
async def test_unattended_preview_failures_are_throttled_and_generic(world, drive, fake_ffmpeg, monkeypatch, settings):
    from mg_archive_bot.bot.jobs import PreviewWorker
    from mg_archive_bot.services.previews import PreviewJob, plan_previews
    from mg_archive_bot.services.validation import validate_project

    h = world
    settings.ffmpeg_path = str(fake_ffmpeg)
    monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
    pid = await _project(h)
    with session_scope() as s:
        p = project_service.get_project(s, pid)
        folder = p.folder("timeline_prores").drive_id
        for i in range(3):
            drive.put_file(folder, f"broken{i}.mov", content=b"x" * 2_000_000, md5=f"b{i}", mime_type="video/quicktime")
        result = await validate_project(s, p, drive, settings)
        jobs = plan_previews(s, p, result.source_files)
    worker = PreviewWorker(h.bot, h.bot_data)
    for job in jobs:
        await worker._process(PreviewJob(job.project_id, job.preview_id, job.source_key, job.source, job.previews_folder_id, None))
    dms = [t for t in h.bot.texts(LEAD.id) if "Preview failed" in t]
    assert len(dms) == 1  # one per project per interval, not one per broken file
    reason = dms[0].split("<i>")[1].split("</i>")[0]
    print("DM:", dms[0])
    assert "boom" not in reason and "/" not in reason and "details are in the bot log" in dms[0]


def test_drive_errors_shown_to_users_are_sanitised():
    import httplib2
    from googleapiclient.errors import HttpError

    from mg_archive_bot.services.drive import GoogleDriveClient

    resp = httplib2.Response({"status": 403, "reason": "Forbidden"})
    err = HttpError(resp, b'{"error": {"errors": [{"reason": "insufficientFilePermissions"}], "message": "The user does not have sufficient permissions for file 1AbCdEf"}}', uri="https://www.googleapis.com/drive/v3/files/1AbCdEf?supportsAllDrives=true&q=%271AbCdEf%27+in+parents")
    text = str(GoogleDriveClient._translate(err))
    assert text.startswith("Google Drive error 403") and "googleapis.com" not in text and "1AbCdEf" not in text and "in+parents" not in text
    transport = str(GoogleDriveClient._translate(ConnectionResetError("Connection reset by peer /Users/x/secrets")))
    assert transport == "Google Drive is unreachable right now (network error); please try again later."


def test_listing_is_bounded_and_truncates_long_names(db, settings, drive):
    import asyncio

    from mg_archive_bot.constants import build_folder_tree
    from mg_archive_bot.services.listing import MAX_FILES, MAX_FOLDERS, build_listing, render_listing

    with session_scope() as s:
        p = project_service.create_draft(s, "Bounded", 1, "Lead", 2026)
        asyncio.run(project_service.provision_folders(s, p, drive, settings))
        fonts, root_id, name = p.folder("fonts").drive_id, p.drive_root_id, p.name
        known = {x.drive_id: x.key for x in p.folders}
    order = {spec.key: i for i, spec in enumerate(build_folder_tree(settings.folder_names()))}
    calls = {"n": 0}
    original = drive.list_children

    def counting(folder_id):
        calls["n"] += 1
        return original(folder_id)

    drive.list_children = counting  # type: ignore[method-assign]
    for i in range(400):
        drive.create_folder(f"sub{i:03d}", fonts)
    drive.put_file(fonts, "x" * 5000 + ".mov", size=1)
    root = build_listing(drive, root_id, name, known, order)
    assert calls["n"] <= MAX_FOLDERS and root.budget_exhausted
    chunks = render_listing("Bounded", root, max_chunks=4)
    assert len(chunks) <= 4 and all(len(c) <= 4096 for c in chunks)
    assert "Listing stopped after" in "".join(chunks) and "see the rest on Google Drive" in chunks[-1]
    assert not any(len(line) > 3600 for c in chunks for line in c.split("\n"))
    assert "x" * 200 not in "".join(chunks)  # long names are cut to 120 characters
    assert MAX_FILES >= 1000


def test_placeholder_or_weak_initial_password_is_refused(db, settings):
    from mg_archive_bot.models import Setting
    from mg_archive_bot.security import hash_password
    from mg_archive_bot.services.users import PASSWORD_KEY

    with session_scope() as s:
        assert not user_service.placeholder_password_in_use(s)
        s.merge(Setting(key=PASSWORD_KEY, value=hash_password("change-me-please")))
    with session_scope() as s:
        assert user_service.placeholder_password_in_use(s)  # startup refuses to run in this state
        s.merge(Setting(key=PASSWORD_KEY, value=""))
    with session_scope() as s:
        for bad in ("change-me-please", "short", "aaaaaaaaaaaaaaaaaa"):
            with pytest.raises(user_service.UserError):
                user_service.seed_password_if_missing(s, bad)
        assert user_service.seed_password_if_missing(s, "A real passphrase 2026")


def test_example_config_values_are_refused(settings):
    from mg_archive_bot.config import Settings

    example = Settings(telegram_bot_token="123456789:replace-with-token-from-BotFather", super_admin_telegram_id=123456789, google_auth_mode="fake", _env_file=None)
    problems = " ".join(example.validate_runtime())
    assert "SUPER_ADMIN_TELEGRAM_ID" in problems and "TELEGRAM_BOT_TOKEN" in problems
    assert settings.validate_runtime() == []


def test_debug_log_level_never_enables_update_logging(settings, tmp_path):
    import logging

    from mg_archive_bot.__main__ import configure_logging

    settings.log_level = "DEBUG"
    settings.log_file = tmp_path / "bot.log"
    configure_logging(settings)
    assert logging.getLogger("mg_archive_bot").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("telegram.ext.Application").isEnabledFor(logging.DEBUG)  # would log full updates
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)  # would log request URLs
    logging.getLogger().handlers.clear()


def test_flood_control_is_configured(settings, drive):
    from telegram.ext import AIORateLimiter

    from mg_archive_bot.bot.app import build_application
    from mg_archive_bot.db import init_db

    init_db(settings.database_url)
    app = build_application(settings, drive)
    assert isinstance(app.bot.rate_limiter, AIORateLimiter)


def test_password_hash_uses_owasp_scrypt_cost(monkeypatch):
    from mg_archive_bot import security

    monkeypatch.setattr(security, "_SCRYPT_P", 5)  # production value (tests otherwise lower it for speed)
    stored = security.hash_password("A real passphrase 2026")
    algo, n, r, p, *_ = stored.split("$")
    assert (algo, int(n), int(r), int(p)) == ("scrypt", 16384, 8, 5)
    assert security.verify_password("A real passphrase 2026", stored)
