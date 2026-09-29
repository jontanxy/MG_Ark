"""Team Lead project flows: /newproject wizard, /projects list and the per-project action menu."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from ...constants import (
    CATEGORY_FLAGS,
    CATEGORY_LABELS,
    METADATA_FIELDS,
    REVOCABLE_STATUSES,
    ROLE_RANK,
    STATUS_LABELS,
    AssetCategory,
    ProjectStatus,
    Role,
)
from ...db import session_scope
from ...models import Collection, Project, User
from ...services import collections as collection_service
from ...services import groups as group_service
from ...services import notifications
from ...services import projects as project_service
from ...services import users as user_service
from ...services.drive import DriveError
from ...services.projects import ProjectError
from ...services.validation import latest_report
from ...util import clip_message, esc
from ..access import (
    clear_prompt,
    drive_of,
    get_prompt,
    parse_enum,
    parse_int,
    placement_lock,
    project_lock,
    require,
    safe_edit,
    set_prompt,
    settings_of,
)
from ..actions import (
    check_project,
    may_list_files,
    notify_user,
    post_to_group,
    project_file_listing,
    rebuild_sheet,
    refresh_live_status_from_latest,
    schedule_sheet_sync,
    sheet_location,
    tree_of,
    user_by_id,
)
from ..keyboards import (
    MAX_COLLECTION_BUTTONS,
    assign_category_keyboard,
    collection_choice_keyboard,
    collection_move_keyboard,
    back_to_project_keyboard,
    confirm_keyboard,
    confirm_revoke_keyboard,
    confirm_verify_keyboard,
    declaration_keyboard,
    group_choice_keyboard,
    lead_choice_keyboard,
    metadata_field_keyboard,
    previews_keyboard,
    project_menu_keyboard,
    projects_list_keyboard,
    user_toggle_keyboard,
    yes_skip_keyboard,
)

log = logging.getLogger(__name__)

WIZARD_META_FIELDS = ["event", "collection", "ministry", "style", "colours", "tags", "description"]
META_HINTS = {
    "event": "e.g. Easter Service, Youth Camp",
    "collection": "e.g. Easter 2026, Christmas Series",
    "ministry": "e.g. Worship, Youth, Kids",
    "style": "e.g. cinematic, minimal, retro",
    "colours": "e.g. gold, navy, white",
    "tags": "comma-separated keywords, e.g. worship, gold, particles",
    "description": "a sentence or two about the look and usage",
    "year": "four digits, e.g. 2026",
    "creator": "designer or team name",
    "asset_types": "e.g. Timeline, Contin Videos",
}
OPEN_STATUSES = (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION)
PAGE_SIZE = 8


def _tz(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["tz"]


MANAGE_ACTIONS = frozenset(
    {"announce", "remind", "assign", "asgcat", "asg", "meta", "mf", "decl", "dt", "group", "grp", "prev",
     "verify", "verify2", "revoke", "revoke2", "restore", "reopen", "lead", "setlead", "rename", "col", "setcol"}
)
NOT_LEAD = "Only this project's lead (or the Super Admin) can do that."


def _menu_text(session: Session, project: Project, context: ContextTypes.DEFAULT_TYPE) -> str:
    report = latest_report(session, project)
    creator = user_by_id(session, project.created_by)
    text = notifications.project_details(project, report, tree_of(context), _tz(context), creator)
    if project.mg_group_chat_id:
        group = group_service.get_group(session, project.mg_group_chat_id)
        state = "" if group is not None and group.is_active else " ⚠️ revoked — relink via “MG group”"
        text += f"\n<b>MG Group:</b> {esc(group.title if group else project.mg_group_chat_id)}{state}"
    else:
        text += "\n<b>MG Group:</b> none linked"
    return clip_message(text)


async def _show_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, prefix: str = "", actor: User | None = None
) -> None:
    manage = actor is None or project_service.can_manage(actor, project)
    await safe_edit(update, prefix + _menu_text(session, project, context), project_menu_keyboard(project, manage=manage))


def _load_project(session: Session, project_id: int) -> Project | None:
    project = project_service.get_project(session, project_id)
    if project is None or project.status == ProjectStatus.DRAFT:
        return None
    return project


# ----------------------------------------------------------------------------------------
# /projects list
# ----------------------------------------------------------------------------------------


def _list_payload(session: Session, page: int, mode: str):
    statuses = (ProjectStatus.ARCHIVED, ProjectStatus.CANCELLED) if mode == "archived" else OPEN_STATUSES
    projects = project_service.list_projects(session, statuses)
    title = "📦 <b>Archived & cancelled projects</b>" if mode == "archived" else "🗂 <b>Open projects</b>"
    if not projects:
        body = "\n\nNothing here yet." + ("" if mode == "archived" else " Create one with /newproject.")
    else:
        total_pages = (len(projects) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        body = f"\n\n{len(projects)} project{'s' if len(projects) != 1 else ''} · page {page + 1}/{total_pages}\nTap a project to manage it."
    return title + body, projects_list_keyboard(projects, page, PAGE_SIZE, mode)


@require(Role.TEAM_LEAD)
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    with session_scope() as session:
        text, kb = _list_payload(session, 0, "open")
        await update.message.reply_text(text, reply_markup=kb)


@require(Role.TEAM_LEAD)
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    args = context.args or []
    project_id = parse_int(args[0]) if args else None
    if project_id is None:
        await update.message.reply_text("Usage: /project &lt;id&gt; — or use /projects to pick one.")
        return
    with session_scope() as session:
        project = _load_project(session, project_id)
        if project is None:
            await update.message.reply_text("Project not found.")
            return
        await update.message.reply_text(
            _menu_text(session, project, context), reply_markup=project_menu_keyboard(project, manage=project_service.can_manage(actor, project))
        )


@require(Role.TEAM_LEAD)
async def projects_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    await query.answer()
    _, page, mode = query.data.split(":")
    with session_scope() as session:
        text, kb = _list_payload(session, int(page), mode)
        await safe_edit(update, text, kb)


# ----------------------------------------------------------------------------------------
# /newproject wizard
# ----------------------------------------------------------------------------------------


@require(Role.TEAM_LEAD)
async def cmd_newproject(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    context.user_data["wizard"] = {}
    with session_scope() as session:
        collections = collection_service.list_collections(session)
        await update.message.reply_text(
            "🆕 <b>New archive</b>\n\n📁 Where should it live?\n"
            "<i>A collection is a folder under the archive root that groups sub-projects (e.g. BF / Opening, BF / Worship).</i>",
            reply_markup=collection_choice_keyboard(collections),
        )


async def _ask_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    wizard = context.user_data.setdefault("wizard", {})
    collection = collection_service.get_collection(session, wizard.get("collection_id"))
    where = f"inside <b>{esc(collection.name)}</b>" if collection else "at the top level"
    set_prompt(context, "project_name")
    text = f"📦 New archive {where}.\n\nSend the <b>project name</b> (e.g. <i>Easter Opening 2026</i>).\n/cancel to abort."
    if update.callback_query is not None:
        await safe_edit(update, text)
    else:
        await update.message.reply_text(text)


async def handle_collection_name(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    if ROLE_RANK[actor.role] < ROLE_RANK[Role.TEAM_LEAD]:
        clear_prompt(context)
        return
    with session_scope() as session:
        try:
            name = project_service.validate_name(update.message.text or "")
            if collection_service.find_by_name(session, name) is None and collection_service.taken_by_top_level_project(session, name):
                raise ProjectError(collection_service.name_taken_message(name))
            collection = collection_service.create_collection(session, name, actor.telegram_id)
        except ProjectError as exc:
            reason = str(exc).replace("Project name", "Collection name")
            await update.message.reply_text(f"❌ {esc(reason)}\nSend another collection name or /cancel.")
            return
        session.commit()
        context.user_data.setdefault("wizard", {})["collection_id"] = collection.id
        await _ask_project_name(update, context, session)


async def handle_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    if ROLE_RANK[actor.role] < ROLE_RANK[Role.TEAM_LEAD]:
        clear_prompt(context)
        return
    name = update.message.text or ""
    wizard = context.user_data.setdefault("wizard", {})
    with session_scope() as session:
        collection = collection_service.get_collection(session, wizard.get("collection_id"))
        try:
            project = project_service.create_draft(
                session, name, actor.telegram_id, actor.display_name, datetime.now(_tz(context)).year, collection
            )
        except ProjectError as exc:
            await update.message.reply_text(f"❌ {esc(str(exc))}\nSend another name or /cancel.")
            return
        session.commit()
        clear_prompt(context)
        wizard["project_id"] = project.id
        await update.message.reply_text(
            f"📦 <b>{esc(project.full_name)}</b>\n\nWhich assets will this project include? Toggle, then Continue.\n"
            "<i>Working File (Fonts, AE) is always required.</i>",
            reply_markup=declaration_keyboard(project, "nw:decl"),
        )


def _load_draft(session: Session, context: ContextTypes.DEFAULT_TYPE, actor: User) -> Project | None:
    wizard = context.user_data.get("wizard") or {}
    project = project_service.get_project(session, wizard.get("project_id", 0))
    if project is None or project.status != ProjectStatus.DRAFT or project.created_by != actor.telegram_id:
        return None
    return project


GROUP_HINT = (
    "<i>Don't see the right group? Send /creategroup to get a token, then add me to that Telegram group "
    "(or send <code>/activate &lt;token&gt;</code> inside it). You can also change the group later from the project menu.</i>"
)


async def _wizard_group_step(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project) -> None:
    """Always let the Team Lead pick the group — a project is never linked to a chat silently."""
    groups = group_service.list_active_groups(session)
    if groups:
        text = f"💬 Which MG Group should receive this project's announcements and progress?\n\n{GROUP_HINT}"
    else:
        text = f"💬 No MG Group is authorised yet, so this project can't be announced anywhere for now.\n\n{GROUP_HINT}"
    await safe_edit(update, text, group_choice_keyboard(groups, "nw:grp"))


async def _wizard_lead_step(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project) -> None:
    leads = project_service.eligible_leads(session)
    if not leads:
        project_service.delete_draft(session, project)
        session.commit()
        context.user_data.pop("wizard", None)
        await safe_edit(update, "👑 Every project needs a Team Lead as its lead, and nobody holds that role yet. Promote someone in /users, then run /newproject again.")
        return
    await safe_edit(update, "👑 Who is the <b>project lead</b>? They manage this project and receive its notifications.", lead_choice_keyboard(leads, "nw:lead"))


async def _wizard_meta_step(update: Update, context: ContextTypes.DEFAULT_TYPE, project: Project, note: str = "") -> None:
    await safe_edit(
        update,
        f"{note}🏷 Add metadata now (event, collection, style, colours, tags…)?\nIt makes the project searchable. You can also add it later.",
        yes_skip_keyboard("nw:meta:yes", "nw:meta:skip"),
    )


async def _ask_wizard_meta(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    field = WIZARD_META_FIELDS[index]
    set_prompt(context, "wizard_meta", index=index)
    text = f"✏️ <b>{METADATA_FIELDS[field]}</b> ({index + 1}/{len(WIZARD_META_FIELDS)})\n<i>{esc(META_HINTS[field])}</i>\n\nSend a value, or <code>-</code> to skip."
    if update.callback_query is not None:
        await update.callback_query.message.reply_text(text)
    else:
        await update.message.reply_text(text)


async def handle_wizard_meta(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    prompt = context.user_data.get("prompt") or {}
    index = int(prompt.get("index", 0))
    field = WIZARD_META_FIELDS[index]
    value = (update.message.text or "").strip()
    with session_scope() as session:
        project = _load_draft(session, context, actor)
        if project is None:
            clear_prompt(context)
            await update.message.reply_text("This wizard has expired. Send /newproject to start again.")
            return
        if value not in {"-", "skip", "/skip"}:
            try:
                project_service.set_metadata_field(session, project, field, value)
            except ProjectError as exc:
                await update.message.reply_text(f"❌ {esc(str(exc))} Try again or send <code>-</code> to skip.")
                return
            session.commit()
        if index + 1 < len(WIZARD_META_FIELDS):
            await _ask_wizard_meta(update, context, index + 1)
            return
        clear_prompt(context)
        await _wizard_assign_step(update, context, session, project)


async def _wizard_assign_step(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project) -> None:
    users = user_service.list_assignable_users(session)
    selected = {a.user_id for a in project.assignments if a.category == AssetCategory.ALL}
    text = "👥 Who is working on this project? Toggle designers, then Done.\n<i>★ = Team Lead, 👁️‍🗨️ = Super Admin. Per-folder assignments can be refined later from the project menu.</i>"
    kb = user_toggle_keyboard(users, selected, "nw:asg", "nw:asg:done")
    if update.callback_query is not None:
        await safe_edit(update, text, kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


async def _wizard_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, note: str = "") -> None:
    declared = [CATEGORY_LABELS[c] for c in project_service.declared_categories(project)]
    group = group_service.get_group(session, project.mg_group_chat_id) if project.mg_group_chat_id else None
    who = project_service.assignees_for(project, None)
    lines = [
        note + f"📋 <b>Ready to create: {esc(project.full_name)}</b>",
        "",
        f"<b>Lead:</b> {esc(project.lead.display_name) if project.lead is not None else '—'}",
        f"<b>Declared assets:</b> {esc(', '.join(declared) if declared else 'Working files only')}",
        f"<b>MG Group:</b> {esc(group.title) if group else 'none — nothing will be announced'}",
        f"<b>Assigned:</b> {esc(', '.join(u.display_name for u in who) if who else 'nobody yet')}",
        f"<b>Tags:</b> {esc(', '.join(project.tag_names) if project.tags else '—')}",
        "",
        "Creating the archive makes the full folder tree on Google Drive and posts the announcement to the MG Group.",
    ]
    await safe_edit(update, "\n".join(lines), confirm_keyboard("nw:confirm", "nw:cancel"))


async def _wizard_create(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, actor: User) -> None:
    await safe_edit(update, "⏳ Creating folders on Google Drive…")
    async with project_lock(context, project.id), placement_lock(context):  # a double tap must not provision two trees
        session.refresh(project)
        if project.status != ProjectStatus.DRAFT:
            context.user_data.pop("wizard", None)
            await _show_menu(update, context, session, project, prefix="✅ <b>Archive already created.</b>\n\n")
            return
        try:
            await project_service.provision_folders(session, project, drive_of(context), settings_of(context))
        except Exception as exc:  # noqa: BLE001 - the draft must stay recoverable whatever went wrong
            session.rollback()
            log.warning("Folder provisioning failed for %s: %s", project.name, exc, exc_info=not isinstance(exc, (ProjectError, DriveError)))
            await safe_edit(
                update,
                f"❌ Could not create folders: {esc(str(exc)[:300])}\n\nFix the problem and retry, or cancel to discard the draft.",
                confirm_keyboard("nw:confirm", "nw:cancel", confirm_text="Retry 🔁"),
            )
            return
        session.commit()
    context.user_data.pop("wizard", None)
    schedule_sheet_sync(context, project.id)
    posted = await post_to_group(context, project.mg_group_chat_id, notifications.announcement(project, tree_of(context)))
    note = "✅ <b>Archive created.</b> "
    if project.mg_group_chat_id and not posted:
        note += "⚠️ I couldn't post the announcement to the MG Group (am I still a member?). "
    elif not project.mg_group_chat_id:
        note += "No MG Group linked — use “MG group” below, then “Announce”. "
    await _show_menu(update, context, session, project, prefix=note + "\n\n")
    log.info("Project %s created by %s", project.name, actor.telegram_id)


@require(Role.TEAM_LEAD)
async def wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    step, arg = parts[1], parts[2] if len(parts) > 2 else ""
    with session_scope() as session:
        if step == "col":  # location step happens before the draft exists
            wizard = context.user_data.setdefault("wizard", {})
            if arg == "new":
                set_prompt(context, "collection_name")
                await safe_edit(update, "📂 Send the <b>collection name</b> (e.g. <i>BF</i>). An existing folder with that name under the archive root is re-used.\n/cancel to abort.")
                return
            if arg == "none":
                wizard["collection_id"] = None
            else:
                collection_id = parse_int(arg)
                if collection_id is None or collection_service.get_collection(session, collection_id) is None:
                    await safe_edit(update, "That collection no longer exists. Send /newproject to start again.")
                    return
                wizard["collection_id"] = collection_id
            await _ask_project_name(update, context, session)
            return
        project = _load_draft(session, context, actor)
        if project is None:
            await safe_edit(update, "This wizard has expired. Send /newproject to start again.")
            return
        if step == "decl":
            if arg == "done":
                if project.lead_id is None:  # the Super Admin created it: a Team Lead must be chosen
                    await _wizard_lead_step(update, context, session, project)
                else:
                    await _wizard_group_step(update, context, session, project)
            else:
                category = parse_enum(AssetCategory, arg)
                if category is None or category not in CATEGORY_FLAGS:
                    return  # malformed button data: ignore
                project_service.toggle_declaration(session, project, category)
                session.commit()
                await query.edit_message_reply_markup(declaration_keyboard(project, "nw:decl"))
        elif step == "lead":
            lead_id = parse_int(arg)
            if lead_id is None:
                return
            try:
                project_service.set_lead(session, project, lead_id)
            except ProjectError as exc:
                await safe_edit(update, f"❌ {esc(str(exc))}")
                return
            session.commit()
            await _wizard_group_step(update, context, session, project)
        elif step == "grp":
            chat_id = None if arg == "none" else parse_int(arg)
            if arg != "none" and chat_id is None:
                return  # malformed button data: ignore
            try:
                project_service.set_group(session, project, chat_id)
            except ProjectError as exc:
                await safe_edit(update, f"❌ {esc(str(exc))}")
                return
            session.commit()
            await _wizard_meta_step(update, context, project)
        elif step == "meta":
            if arg == "yes":
                await safe_edit(update, "🏷 Let's add metadata. Answer each prompt or send <code>-</code> to skip.")
                await _ask_wizard_meta(update, context, 0)
            else:
                await _wizard_assign_step(update, context, session, project)
        elif step == "asg":
            if arg == "done":
                await _wizard_summary(update, context, session, project)
            else:
                user_id = parse_int(arg)
                if user_id is None:
                    return  # malformed button data: ignore
                try:
                    project_service.toggle_assignment(session, project, user_id, AssetCategory.ALL)
                except ProjectError:
                    return
                session.commit()
                users = user_service.list_assignable_users(session)
                selected = {a.user_id for a in project.assignments if a.category == AssetCategory.ALL}
                await query.edit_message_reply_markup(user_toggle_keyboard(users, selected, "nw:asg", "nw:asg:done"))
        elif step == "confirm":
            await _wizard_create(update, context, session, project, actor)
        elif step == "cancel":
            project_service.delete_draft(session, project)
            session.commit()
            context.user_data.pop("wizard", None)
            clear_prompt(context)
            await safe_edit(update, "Draft discarded.")


# ----------------------------------------------------------------------------------------
# Project action menu
# ----------------------------------------------------------------------------------------


def _metadata_text(project: Project) -> str:
    lines = [f"🏷 <b>Metadata — {esc(project.full_name)}</b>", ""]
    for field, label in METADATA_FIELDS.items():
        value = ", ".join(project.tag_names) if field == "tags" else getattr(project, field)
        lines.append(f"<b>{label}:</b> {esc(value) if value else '—'}")
    lines += ["", "Tap a field to edit it."]
    return "\n".join(lines)


async def handle_meta_value(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    prompt = context.user_data.get("prompt") or {}
    project_id, field = int(prompt.get("project_id", 0)), prompt.get("field", "")
    if ROLE_RANK[actor.role] < ROLE_RANK[Role.TEAM_LEAD]:
        clear_prompt(context)
        return
    with session_scope() as session:
        project = _load_project(session, project_id)
        if project is None or field not in METADATA_FIELDS:
            clear_prompt(context)
            await update.message.reply_text("That project is no longer available.")
            return
        if not project_service.can_manage(actor, project):
            clear_prompt(context)
            await update.message.reply_text(NOT_LEAD)
            return
        try:
            stored = project_service.set_metadata_field(session, project, field, update.message.text or "")
        except ProjectError as exc:
            await update.message.reply_text(f"❌ {esc(str(exc))} Try again or /cancel.")
            return
        session.commit()
        clear_prompt(context)
        schedule_sheet_sync(context, project.id)
        await update.message.reply_text(
            f"✅ <b>{METADATA_FIELDS[field]}</b> set to: {esc(stored) if stored else '—'}\n\n" + _metadata_text(project),
            reply_markup=metadata_field_keyboard(project, METADATA_FIELDS),
        )


async def handle_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    prompt = context.user_data.get("prompt") or {}
    project_id = int(prompt.get("project_id", 0))
    if ROLE_RANK[actor.role] < ROLE_RANK[Role.TEAM_LEAD]:
        clear_prompt(context)
        return
    with session_scope() as session:
        project = _load_project(session, project_id)
        if project is None:
            clear_prompt(context)
            await update.message.reply_text("That project is no longer available.")
            return
        if not project_service.can_manage(actor, project):
            clear_prompt(context)
            await update.message.reply_text(NOT_LEAD)
            return
        requested = " ".join((update.message.text or "").split())[:100]
        await _say_if_waiting(update, context, project.id)
        async with project_lock(context, project.id), placement_lock(context):
            if not _prompt_pending(context, prompt):
                await update.message.reply_text(
                    f"ℹ️ <b>{esc(project.full_name)}</b> was not renamed to “{esc(requested)}”: that step was closed before its turn came. "
                    "Press Rename again to repeat it."
                )
                return
            session.refresh(project)
            try:
                old, new = await project_service.rename_project(session, project, update.message.text or "", drive_of(context), settings_of(context))
            except ProjectError as exc:
                await update.message.reply_text(f"❌ {esc(str(exc))} Send another name or /cancel.")
                return
            except DriveError as exc:
                session.rollback()
                await update.message.reply_text(f"❌ Google Drive refused to rename the folder: {esc(str(exc))}. The project keeps its name.")
                return
            session.commit()
            _close_prompt(context, prompt)
        schedule_sheet_sync(context, project.id)
        await refresh_live_status_from_latest(context, session, project)
        await post_to_group(context, project.mg_group_chat_id, notifications.renamed_message(old, project))
        await update.message.reply_text(
            f"✅ Renamed <b>{esc(old)}</b> → <b>{esc(project.full_name)}</b>.\n\n" + _menu_text(session, project, context),
            reply_markup=project_menu_keyboard(project, manage=True),
        )
        log.info("Project %s renamed to %s by %s", old, project.name, actor.telegram_id)


MOVED, UNCHANGED, REFUSED, FAILED, ABANDONED = "moved", "unchanged", "refused", "failed", "abandoned"


def _prompt_pending(context: ContextTypes.DEFAULT_TYPE, prompt: dict | None) -> bool:
    """Whether the text step this handler was started for is still the open one. A /cancel, a command, a button
    press, its expiry, and the same step opened once more (that is a new prompt) all close it."""
    return bool(prompt) and get_prompt(context) is prompt


def _close_prompt(context: ContextTypes.DEFAULT_TYPE, prompt: dict | None) -> None:
    """Close the text step that was just carried out, but not one that the user has opened since."""
    if prompt and context.user_data.get("prompt") is prompt:
        clear_prompt(context)


async def _say_if_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: int) -> None:
    """A rename, move or new archive by somebody else is in progress: say so instead of staying silent."""
    if placement_lock(context).locked() or project_lock(context, project_id).locked():
        await update.effective_message.reply_text("⏳ Another change to the archive is in progress. Yours is next, one moment…")


def _collection_menu_text(project: Project, collections: list) -> str:
    where = f"in the collection <b>{esc(project.collection_folder.name)}</b>" if project.collection_folder is not None else "at the top level (in no collection)"
    lines = [
        f"📂 <b>{esc(project.name)}</b> is {where}.",
        "",
        "Where should it live? Its Google Drive folder is moved too; links keep working and nothing inside the folder changes.",
    ]
    if project.collection_id is None and project.collection:
        lines.append(f"<i>Its Collection label “{esc(project.collection)}” will be replaced by the name of the collection.</i>")
    offered = [c for c in collections if c.id != project.collection_id]
    if len(offered) > MAX_COLLECTION_BUTTONS:
        lines.append(
            f"<i>Showing {MAX_COLLECTION_BUTTONS} of {len(offered)} collections. For one that is not listed, press "
            "“New collection…” and send its name: an existing collection is re-used.</i>"
        )
    return "\n".join(lines)


async def _move_project(
    context: ContextTypes.DEFAULT_TYPE,
    session: Session,
    project: Project,
    actor: User,
    target_id: int | None,
    *,
    new_name: str | None = None,
    prompt: dict | None = None,
) -> tuple[str, str]:
    """Move *project* into the collection *target_id* (``None`` = top level), or into the collection called
    *new_name*, which is created when it does not exist (the text prompt).

    Returns (MOVED, html), (UNCHANGED, html: it is there already), (REFUSED, plain reason: the request itself
    cannot be done), (FAILED, plain reason: Google Drive did not do it; the request can be repeated) or
    (ABANDONED, ""): the text step was closed while this handler was waiting for its turn.
    """
    async with project_lock(context, project.id), placement_lock(context):
        if new_name is not None and not _prompt_pending(context, prompt):
            return ABANDONED, ""
        session.refresh(project)
        if new_name is not None:
            # looked up under the lock; a new collection is only stored once the move has happened
            target = collection_service.find_by_name(session, new_name) or Collection(name=new_name, created_by=actor.telegram_id)
        else:
            target = collection_service.get_collection(session, target_id)
            if target_id is not None and target is None:
                return REFUSED, "That collection no longer exists."
        old_collection = project.collection_folder.name if project.collection_folder is not None else None
        if target is not None and target.id is not None and project.collection_id == target.id:
            return UNCHANGED, f"ℹ️ <b>{esc(project.name)}</b> is already in <b>{esc(target.name)}</b>."
        try:
            old_display = await project_service.move_project(session, project, target, drive_of(context), settings_of(context))
            session.commit()
        except ProjectError as exc:
            session.rollback()
            return REFUSED, str(exc)
        except DriveError as exc:
            session.rollback()
            return FAILED, f"Google Drive did not confirm the move: {exc}. Nothing was changed in the bot; you can try again."
        except SQLAlchemyError:
            session.rollback()
            log.exception("The move of project %s could not be saved", project.id)
            return FAILED, (
                "The bot could not save the change. If the Google Drive folder was moved already, "
                "choosing the same collection again puts the record right."
            )
        if new_name is not None:
            _close_prompt(context, prompt)
    schedule_sheet_sync(context, project.id)
    await refresh_live_status_from_latest(context, session, project)
    await post_to_group(context, project.mg_group_chat_id, notifications.moved_message(old_collection, project))
    log.info("Project %s (%s) is now %s, moved by %s", project.id, old_display, project.full_name, actor.telegram_id)
    if project.collection_folder is None:
        return MOVED, f"✅ <b>{esc(project.name)}</b> was taken out of <b>{esc(old_collection or '')}</b> and now sits at the top level."
    return MOVED, f"✅ <b>{esc(project.name)}</b> is now in <b>{esc(project.collection_folder.name)}</b>."


async def handle_move_collection_name(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    """The name typed after “New collection…” in a project's Collection menu."""
    prompt = context.user_data.get("prompt") or {}
    project_id = int(prompt.get("project_id", 0))
    if ROLE_RANK[actor.role] < ROLE_RANK[Role.TEAM_LEAD]:
        clear_prompt(context)
        return
    with session_scope() as session:
        project = _load_project(session, project_id)
        if project is None:
            clear_prompt(context)
            await update.message.reply_text("That project is no longer available.")
            return
        if not project_service.can_manage(actor, project):
            clear_prompt(context)
            await update.message.reply_text(NOT_LEAD)
            return
        if project.status == ProjectStatus.CANCELLED:
            clear_prompt(context)
            await update.message.reply_text("This project was cancelled in the meantime. Restore it before moving it.")
            return
        try:
            name = project_service.validate_name(update.message.text or "")
        except ProjectError as exc:
            reason = str(exc).replace("Project name", "Collection name")
            await update.message.reply_text(f"❌ {esc(reason)}\nSend another collection name or /cancel.")
            return
        await _say_if_waiting(update, context, project.id)
        outcome, message = await _move_project(context, session, project, actor, None, new_name=name, prompt=prompt)
        if outcome == ABANDONED:
            await update.message.reply_text(
                f"ℹ️ <b>{esc(project.name)}</b> was not moved to “{esc(name)}”: that step was closed before its turn came. "
                "Open Collection again to repeat it."
            )
            return
        if outcome == UNCHANGED:
            await update.message.reply_text(f"{message}\nSend another collection name or /cancel.")
            return
        if outcome != MOVED:
            hint = "Send another collection name or /cancel." if outcome == REFUSED else "Send the name again to retry, or /cancel."
            await update.message.reply_text(f"❌ {esc(message)}\n{hint}")
            return
        await update.message.reply_text(
            f"{message}\n\n" + _menu_text(session, project, context), reply_markup=project_menu_keyboard(project, manage=True)
        )


async def _assign_users_view(update: Update, session: Session, project: Project, category: AssetCategory) -> None:
    users = user_service.list_assignable_users(session)
    selected = {a.user_id for a in project.assignments if a.category == category}
    await safe_edit(
        update,
        f"👥 <b>{esc(project.full_name)}</b> — assign for <b>{CATEGORY_LABELS[category]}</b>\nToggle users, then Done.",
        user_toggle_keyboard(users, selected, f"pj:{project.id}:asg:{category.value}", f"pj:{project.id}:assign"),
    )


async def _progress_view(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, actor: User, *, prefix: str = "") -> None:
    outcome = await check_project(context, session, project, requested_by=actor.telegram_id)
    text = notifications.progress_message(project, outcome.result.report, tree_of(context), _tz(context))
    if outcome.queued_previews:
        text += f"\n🎞 Queued {outcome.queued_previews} preview{'s' if outcome.queued_previews != 1 else ''} for generation."
    if outcome.result.became_ready:
        text += "\n\n🔵 Ready for verification" + (" — the MG Group has been notified." if outcome.group_notified else ".")
    await safe_edit(update, prefix + text, project_menu_keyboard(project))


@require(Role.TEAM_LEAD)
async def project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    project_id = parse_int(parts[1] if len(parts) > 1 else None)
    action, args = (parts[2] if len(parts) > 2 else ""), parts[3:]
    if project_id is None:
        await query.answer("Invalid request.", show_alert=True)
        return
    settings = settings_of(context)
    with session_scope() as session:
        project = _load_project(session, project_id)
        if project is None:
            await query.answer()
            await safe_edit(update, "Project not found.")
            return
        if action in MANAGE_ACTIONS and not project_service.can_manage(actor, project):
            await query.answer(NOT_LEAD, show_alert=True)
            return

        if action == "menu":
            await query.answer()
            await _show_menu(update, context, session, project, actor=actor)

        elif action == "details":
            await query.answer()
            await _show_menu(update, context, session, project, actor=actor)

        elif action == "lead":
            await query.answer()
            leads = project_service.eligible_leads(session)
            current = esc(project.lead.display_name) if project.lead is not None else "none"
            await safe_edit(
                update,
                f"👑 <b>{esc(project.full_name)}</b> — project lead\nCurrent: <b>{current}</b>\n\nThe lead manages the project and receives its notifications. Only Team Leads can be chosen.",
                lead_choice_keyboard(leads, f"pj:{project.id}:setlead", back_data=f"pj:{project.id}:menu"),
            )

        elif action == "setlead":
            lead_id = parse_int(args[0] if args else None)
            if lead_id is None:
                await query.answer("Invalid request.", show_alert=True)
                return
            try:
                lead = project_service.set_lead(session, project, lead_id)
            except ProjectError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer(f"Lead: {lead.display_name}")
            if lead.telegram_id != actor.telegram_id:
                await notify_user(context, lead.telegram_id, f"👑 You are now the lead of <b>{esc(project.full_name)}</b>. Open /projects to manage it.")
            await _show_menu(update, context, session, project, actor=actor)

        elif action == "files":
            if project.status == ProjectStatus.CANCELLED:
                await query.answer("This project's folder is in the Drive trash.", show_alert=True)
                return
            if not may_list_files(context, actor.telegram_id):
                await query.answer("Please wait a minute before requesting another file listing.", show_alert=True)
                return
            await query.answer("Reading Google Drive…")
            for chunk in await project_file_listing(context, project):
                await context.bot.send_message(update.effective_chat.id, chunk)

        elif action == "check":
            await query.answer("Checking Google Drive…")
            await _progress_view(update, context, session, project, actor)

        elif action == "announce":
            if not project.mg_group_chat_id:
                await query.answer("Link an MG Group first (💬 MG group).", show_alert=True)
                return
            ok = await post_to_group(context, project.mg_group_chat_id, notifications.announcement(project, tree_of(context)))
            await query.answer("📣 Announcement posted." if ok else "Could not post — is the bot still in the group?", show_alert=not ok)

        elif action == "remind":
            if not project.mg_group_chat_id:
                await query.answer("Link an MG Group first (💬 MG group).", show_alert=True)
                return
            await query.answer("Checking Google Drive…")
            outcome = await check_project(context, session, project, requested_by=actor.telegram_id)
            if outcome.result.report.had_errors:
                await _show_menu(update, context, session, project, prefix="⚠️ Google Drive could not be checked — no reminder sent.\n\n")
                return
            missing = outcome.result.report.missing
            if not missing:
                await _show_menu(update, context, session, project, prefix="✅ Nothing is missing — no reminder needed.\n\n")
                return
            ok = await post_to_group(context, project.mg_group_chat_id, notifications.reminder_message(project, outcome.result.report, tree_of(context)))
            if ok:
                project_service.mark_reminded(session, project)
                session.commit()
            await _show_menu(update, context, session, project, prefix="⏰ Reminder posted to the MG Group.\n\n" if ok else "⚠️ Could not post to the MG Group (is it still authorised and is the bot a member?).\n\n")

        elif action == "assign":
            await query.answer()
            await safe_edit(update, f"👥 <b>{esc(project.full_name)}</b> — assign designers\nPick what to assign for. “All assets” covers every folder.", assign_category_keyboard(project))

        elif action == "asgcat":
            category = parse_enum(AssetCategory, args[0] if args else None)
            if category is None:
                await query.answer("Invalid request.", show_alert=True)
                return
            await query.answer()
            await _assign_users_view(update, session, project, category)

        elif action == "asg":
            category = parse_enum(AssetCategory, args[0] if args else None)
            user_id = parse_int(args[1] if len(args) > 1 else None)
            if category is None or user_id is None:
                await query.answer("Invalid request.", show_alert=True)
                return
            try:
                now_on = project_service.toggle_assignment(session, project, user_id, category)
            except ProjectError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer("Assigned" if now_on else "Unassigned")
            users = user_service.list_assignable_users(session)
            selected = {a.user_id for a in project.assignments if a.category == category}
            await query.edit_message_reply_markup(
                user_toggle_keyboard(users, selected, f"pj:{project.id}:asg:{category.value}", f"pj:{project.id}:assign")
            )

        elif action == "meta":
            await query.answer()
            await safe_edit(update, _metadata_text(project), metadata_field_keyboard(project, METADATA_FIELDS))

        elif action == "rename":
            if project.status in (ProjectStatus.CANCELLED,):
                await query.answer("Cancelled projects cannot be renamed.", show_alert=True)
                return
            await query.answer()
            set_prompt(context, "rename", project_id=project.id)
            await query.message.reply_text(
                f"✏️ <b>Rename {esc(project.full_name)}</b>\n\nSend the new project name (2–100 characters). The Google Drive folder is renamed to match; links keep working.\n/cancel to abort."
            )

        elif action == "col":
            if project.status == ProjectStatus.CANCELLED:
                await query.answer("Restore the project before moving it.", show_alert=True)
                return
            await query.answer()
            collections = collection_service.list_collections(session)
            await safe_edit(update, _collection_menu_text(project, collections), collection_move_keyboard(project, collections))

        elif action == "setcol":
            arg = args[0] if args else ""
            if project.status == ProjectStatus.CANCELLED:
                await query.answer("Restore the project before moving it.", show_alert=True)
                return
            if arg == "new":
                await query.answer()
                set_prompt(context, "move_collection", project_id=project.id)
                await query.message.reply_text(
                    f"📂 Send the <b>collection name</b> for <b>{esc(project.name)}</b> (e.g. <i>BF</i>). "
                    "An existing collection with that name is re-used.\n/cancel to abort."
                )
                return
            target_id = None if arg == "none" else parse_int(arg)
            if arg != "none" and target_id is None:
                await query.answer("Invalid request.", show_alert=True)
                return
            await query.answer()  # acknowledged now: waiting for Google Drive can take longer than Telegram allows
            await safe_edit(update, f"⏳ Moving <b>{esc(project.name)}</b> and its Google Drive folder…")  # no buttons meanwhile
            outcome, message = await _move_project(context, session, project, actor, target_id)
            if outcome in (REFUSED, FAILED):
                collections = collection_service.list_collections(session)
                await safe_edit(
                    update, f"❌ {esc(message)}\n\n" + _collection_menu_text(project, collections), collection_move_keyboard(project, collections)
                )
                return
            await _show_menu(update, context, session, project, prefix=f"{message}\n\n", actor=actor)

        elif action == "mf":
            field = args[0]
            if field not in METADATA_FIELDS:
                await query.answer("Unknown field", show_alert=True)
                return
            await query.answer()
            set_prompt(context, "meta_value", project_id=project.id, field=field)
            current = ", ".join(project.tag_names) if field == "tags" else getattr(project, field)
            await query.message.reply_text(
                f"✏️ <b>{METADATA_FIELDS[field]}</b> for <b>{esc(project.full_name)}</b>\n<i>{esc(META_HINTS.get(field, ''))}</i>\n"
                f"Current: {esc(current) if current else '—'}\n\nSend the new value, <code>-</code> to clear, or /cancel."
            )

        elif action == "decl":
            await query.answer()
            await safe_edit(
                update,
                f"⚙️ <b>{esc(project.full_name)}</b> — declared assets\nOnly declared assets are validated. Working File (Fonts, AE) is always required.",
                declaration_keyboard(project, f"pj:{project.id}:dt", done_text="◀️ Back to project"),
            )

        elif action == "dt":
            arg = args[0] if args else ""
            if arg == "done":
                await query.answer()
                await _show_menu(update, context, session, project)
                return
            category = parse_enum(AssetCategory, arg)
            if category is None or category not in CATEGORY_FLAGS:
                await query.answer("Invalid request.", show_alert=True)
                return
            created: list[str] = []
            toast, alert = "Saved", False
            async with project_lock(context, project.id):  # a double tap must not create duplicate folders
                session.refresh(project)
                project_service.toggle_declaration(session, project, category)
                session.commit()
                if project.drive_root_id:
                    try:
                        created = await project_service.ensure_folders(session, project, drive_of(context), settings)
                        session.commit()
                        if created:
                            toast = "Folder created on Drive"
                    except (ProjectError, DriveError) as exc:
                        session.rollback()
                        log.warning("ensure_folders failed: %s", exc)
                        toast, alert = f"Saved, but Drive folder creation failed: {exc}"[:190], True
            schedule_sheet_sync(context, project.id)
            await query.answer(toast, show_alert=alert)
            await query.edit_message_reply_markup(declaration_keyboard(project, f"pj:{project.id}:dt", done_text="◀️ Back to project"))

        elif action == "group":
            await query.answer()
            groups = group_service.list_active_groups(session)
            if not groups:
                await safe_edit(update, f"No MG Group is authorised yet.\n\n{GROUP_HINT}", back_to_project_keyboard(project.id))
                return
            current = group_service.get_group(session, project.mg_group_chat_id) if project.mg_group_chat_id else None
            now = f"Currently: <b>{esc(current.title)}</b>\n\n" if current else ""
            await safe_edit(update, f"💬 Which MG Group should receive this project's announcements?\n{now}{GROUP_HINT}", group_choice_keyboard(groups, f"pj:{project.id}:grp"))

        elif action == "grp":
            arg = args[0] if args else ""
            chat_id = None if arg == "none" else parse_int(arg)
            if arg != "none" and chat_id is None:
                await query.answer("Invalid request.", show_alert=True)
                return
            try:
                project_service.set_group(session, project, chat_id)
            except ProjectError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer("MG Group updated")
            await _show_menu(update, context, session, project)

        elif action == "prev":
            if not settings.previews_enabled:
                await query.answer("Preview generation is disabled in the configuration.", show_alert=True)
                return
            await query.answer("Scanning ProRes folders…")
            outcome = await check_project(context, session, project, requested_by=actor.telegram_id, force_previews=True)
            n = outcome.queued_previews
            sources = sum(len(v) for v in outcome.result.source_files.values())
            if n:
                note = f"🎞 Queued {n} preview{'s' if n != 1 else ''}. I'll message you as each one is ready.\n\n"
            elif sources:
                note = "🎞 All previews are already up to date.\n\n"
            else:
                note = "🎞 No ProRes video files found in the declared Timeline / Contin Videos folders yet.\n\n"
            await _show_menu(update, context, session, project, prefix=note)

        elif action == "previews":
            ready = project.ready_previews
            if not ready:
                await query.answer("No previews generated yet.", show_alert=True)
                return
            await query.answer()
            await safe_edit(update, f"▶️ <b>{esc(project.full_name)}</b> — previews on Google Drive\nTap one to play it.", previews_keyboard(project, back_data=f"pj:{project.id}:menu"))

        elif action == "verify":
            await query.answer("Re-checking Google Drive…")
            outcome = await check_project(context, session, project, requested_by=actor.telegram_id)
            if project.status != ProjectStatus.READY_FOR_VERIFICATION:
                text = notifications.progress_message(project, outcome.result.report, tree_of(context), _tz(context))
                await safe_edit(update, "⚠️ <b>Not ready</b> — some declared assets are still missing.\n\n" + text, project_menu_keyboard(project))
                return
            await safe_edit(
                update,
                f"✅ All declared assets are present for <b>{esc(project.full_name)}</b>.\n\nMark it as <b>ARCHIVED</b>? This closes the archive and announces completion in the MG Group.",
                confirm_verify_keyboard(project.id),
            )

        elif action == "verify2":
            async with project_lock(context, project.id):
                session.refresh(project)
                try:
                    project_service.verify_project(session, project, actor.telegram_id)
                except ProjectError as exc:
                    await query.answer(str(exc), show_alert=True)
                    return
                session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer("Archived ✅")
            verifier = user_by_id(session, actor.telegram_id)
            await refresh_live_status_from_latest(context, session, project)
            await post_to_group(context, project.mg_group_chat_id, notifications.archived_message(project, verifier))
            await _show_menu(update, context, session, project, prefix="✅ <b>Archived.</b>\n\n")
            log.info("Project %s archived by %s", project.name, actor.telegram_id)

        elif action == "revoke":
            if project.status not in REVOCABLE_STATUSES:
                await query.answer("Only active, incomplete or ready projects can be revoked.", show_alert=True)
                return
            await query.answer()
            report = latest_report(session, project)
            files = sum(i.file_count or 0 for i in report.required_items) if report else 0
            await safe_edit(
                update,
                f"🗑 <b>Revoke {esc(project.full_name)}?</b>\n\n"
                f"• Its Google Drive folder (last check: {files} file{'s' if files != 1 else ''} in required folders) is moved to the <b>trash</b>.\n"
                "• Tracking, reminders and previews stop; it disappears from search.\n"
                "• The MG Group is notified.\n\n"
                "A Drive manager can recover the folder from the trash for 30 days, and “Restore project” undoes this.",
                confirm_revoke_keyboard(project.id),
            )

        elif action == "revoke2":
            async with project_lock(context, project.id):
                session.refresh(project)
                try:
                    trashed = await project_service.revoke_project(session, project, drive_of(context), actor.telegram_id)
                except ProjectError as exc:
                    await query.answer(str(exc), show_alert=True)
                    return
                except DriveError as exc:
                    session.rollback()
                    await query.answer(f"Drive refused to trash the folder: {exc}"[:190], show_alert=True)
                    return
                session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer("Project revoked")
            await refresh_live_status_from_latest(context, session, project)
            await post_to_group(context, project.mg_group_chat_id, notifications.cancelled_message(project, user_by_id(session, actor.telegram_id), trashed))
            await _show_menu(update, context, session, project, prefix="🗑 <b>Revoked.</b> " + ("The Drive folder is in the trash.\n\n" if trashed else "\n\n"))
            log.info("Project %s revoked by %s", project.full_name, actor.telegram_id)

        elif action == "restore":
            async with project_lock(context, project.id):
                session.refresh(project)
                try:
                    await project_service.restore_project(session, project, drive_of(context))
                except ProjectError as exc:
                    await query.answer(str(exc), show_alert=True)
                    return
                except DriveError as exc:
                    session.rollback()
                    await query.answer(f"Drive could not restore the folder: {exc}"[:190], show_alert=True)
                    return
                session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer("Restored")
            await refresh_live_status_from_latest(context, session, project)
            await post_to_group(context, project.mg_group_chat_id, notifications.restored_message(project))
            await _show_menu(update, context, session, project, prefix="♻️ <b>Restored.</b>\n\n")

        elif action == "reopen":
            async with project_lock(context, project.id):
                session.refresh(project)
                try:
                    project_service.reopen_project(session, project)
                except ProjectError as exc:
                    await query.answer(str(exc), show_alert=True)
                    return
                session.commit()
            schedule_sheet_sync(context, project.id)
            await query.answer("Reopened")
            await refresh_live_status_from_latest(context, session, project)
            await post_to_group(context, project.mg_group_chat_id, notifications.reopened_message(project))
            await _show_menu(update, context, session, project, prefix="🔓 <b>Reopened.</b>\n\n")

        else:
            await query.answer("Unknown action", show_alert=True)


def status_label(project: Project) -> str:
    return STATUS_LABELS[project.status]


@require(Role.TEAM_LEAD)
async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    """Link to the project index sheet; ``/sheet rebuild`` rewrites it from the database."""
    if not settings_of(context).tracking_sheet_enabled:
        await update.message.reply_text("The project index sheet is disabled in the configuration.")
        return
    args = [a.lower() for a in (context.args or [])]
    try:
        if args and args[0] == "rebuild":
            count, url = await rebuild_sheet(context)
            await update.message.reply_text(f"🔄 Index rebuilt: {count} project{'s' if count != 1 else ''}.\n{url}")
            return
        _, url = await sheet_location(context)
    except Exception as exc:  # noqa: BLE001 - surface the reason instead of a generic error
        await update.message.reply_text(f"❌ Could not reach the project index sheet: {esc(str(exc)[:400])}")
        return
    with session_scope() as session:
        total = len(project_service.list_projects(session, [st for st in ProjectStatus if st != ProjectStatus.DRAFT]))
    await update.message.reply_text(
        f"📋 <b>Project index</b> — {total} project{'s' if total != 1 else ''}, one row each, updated automatically.\n{url}\n\n"
        "Send <code>/sheet rebuild</code> to rewrite it from the database."
    )
