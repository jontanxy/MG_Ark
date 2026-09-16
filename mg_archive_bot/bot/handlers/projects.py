"""Team Lead project flows: /newproject wizard, /projects list and the per-project action menu."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from ...constants import (
    CATEGORY_LABELS,
    METADATA_FIELDS,
    ROLE_RANK,
    STATUS_LABELS,
    AssetCategory,
    ProjectStatus,
    Role,
)
from ...db import session_scope
from ...models import Project, User
from ...services import collections as collection_service
from ...services import groups as group_service
from ...services import notifications
from ...services import projects as project_service
from ...services import users as user_service
from ...services.drive import DriveError
from ...services.projects import ProjectError
from ...services.validation import latest_report
from ...util import esc
from ..access import clear_prompt, drive_of, project_lock, require, safe_edit, set_prompt, settings_of
from ..actions import check_project, post_to_group, tree_of, user_by_id
from ..keyboards import (
    assign_category_keyboard,
    collection_choice_keyboard,
    back_to_project_keyboard,
    confirm_keyboard,
    confirm_verify_keyboard,
    declaration_keyboard,
    group_choice_keyboard,
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
    return text


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, prefix: str = "") -> None:
    await safe_edit(update, prefix + _menu_text(session, project, context), project_menu_keyboard(project))


def _load_project(session: Session, project_id: int) -> Project | None:
    project = project_service.get_project(session, project_id)
    if project is None or project.status == ProjectStatus.DRAFT:
        return None
    return project


# ----------------------------------------------------------------------------------------
# /projects list
# ----------------------------------------------------------------------------------------


def _list_payload(session: Session, page: int, mode: str):
    statuses = (ProjectStatus.ARCHIVED,) if mode == "archived" else OPEN_STATUSES
    projects = project_service.list_projects(session, statuses)
    title = "📦 <b>Archived projects</b>" if mode == "archived" else "🗂 <b>Open projects</b>"
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
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /project &lt;id&gt; — or use /projects to pick one.")
        return
    with session_scope() as session:
        project = _load_project(session, int(args[0]))
        if project is None:
            await update.message.reply_text("Project not found.")
            return
        await update.message.reply_text(_menu_text(session, project, context), reply_markup=project_menu_keyboard(project))


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
            collection = collection_service.create_collection(session, update.message.text or "", actor.telegram_id)
        except ProjectError as exc:
            await update.message.reply_text(f"❌ {esc(str(exc))}\nSend another collection name or /cancel.")
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


async def _wizard_group_step(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project) -> None:
    groups = group_service.list_active_groups(session)
    if len(groups) > 1:
        await safe_edit(update, "💬 Which MG Group should receive announcements and progress?", group_choice_keyboard(groups, "nw:grp"))
        return
    project_service.set_group(session, project, groups[0].chat_id if groups else None)
    session.commit()
    await _wizard_meta_step(update, context, project, note="" if groups else "ℹ️ No MG Group is authorised yet — you can link one later from the project menu.\n\n")


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
    users = user_service.list_active_users(session)
    selected = {a.user_id for a in project.assignments if a.category == AssetCategory.ALL}
    text = "👥 Who is working on this project? Toggle designers, then Done.\n<i>★ = Team Lead. Per-folder assignments can be refined later from the project menu.</i>"
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
        f"<b>Declared assets:</b> {esc(', '.join(declared) if declared else 'Working files only')}",
        f"<b>MG Group:</b> {esc(group.title) if group else 'none'}",
        f"<b>Assigned:</b> {esc(', '.join(u.display_name for u in who) if who else 'nobody yet')}",
        f"<b>Tags:</b> {esc(', '.join(project.tag_names) if project.tags else '—')}",
        "",
        "Creating the archive makes the full folder tree on Google Drive and posts the announcement to the MG Group.",
    ]
    await safe_edit(update, "\n".join(lines), confirm_keyboard("nw:confirm", "nw:cancel"))


async def _wizard_create(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session, project: Project, actor: User) -> None:
    await safe_edit(update, "⏳ Creating folders on Google Drive…")
    async with project_lock(context, project.id):  # a double tap must not provision two trees
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
                if collection_service.get_collection(session, int(arg)) is None:
                    await safe_edit(update, "That collection no longer exists. Send /newproject to start again.")
                    return
                wizard["collection_id"] = int(arg)
            await _ask_project_name(update, context, session)
            return
        project = _load_draft(session, context, actor)
        if project is None:
            await safe_edit(update, "This wizard has expired. Send /newproject to start again.")
            return
        if step == "decl":
            if arg == "done":
                await _wizard_group_step(update, context, session, project)
            else:
                project_service.toggle_declaration(session, project, AssetCategory(arg))
                session.commit()
                await query.edit_message_reply_markup(declaration_keyboard(project, "nw:decl"))
        elif step == "grp":
            try:
                project_service.set_group(session, project, None if arg == "none" else int(arg))
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
                project_service.toggle_assignment(session, project, int(arg), AssetCategory.ALL)
                session.commit()
                users = user_service.list_active_users(session)
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
        try:
            stored = project_service.set_metadata_field(session, project, field, update.message.text or "")
        except ProjectError as exc:
            await update.message.reply_text(f"❌ {esc(str(exc))} Try again or /cancel.")
            return
        session.commit()
        clear_prompt(context)
        await update.message.reply_text(
            f"✅ <b>{METADATA_FIELDS[field]}</b> set to: {esc(stored) if stored else '—'}\n\n" + _metadata_text(project),
            reply_markup=metadata_field_keyboard(project, METADATA_FIELDS),
        )


async def _assign_users_view(update: Update, session: Session, project: Project, category: AssetCategory) -> None:
    users = user_service.list_active_users(session)
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
    project_id, action, args = int(parts[1]), parts[2], parts[3:]
    settings = settings_of(context)
    with session_scope() as session:
        project = _load_project(session, project_id)
        if project is None:
            await query.answer()
            await safe_edit(update, "Project not found.")
            return

        if action == "menu":
            await query.answer()
            await _show_menu(update, context, session, project)

        elif action == "details":
            await query.answer()
            await _show_menu(update, context, session, project)

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
            await query.answer()
            await _assign_users_view(update, session, project, AssetCategory(args[0]))

        elif action == "asg":
            category, user_id = AssetCategory(args[0]), int(args[1])
            try:
                now_on = project_service.toggle_assignment(session, project, user_id, category)
            except ProjectError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            session.commit()
            await query.answer("Assigned" if now_on else "Unassigned")
            users = user_service.list_active_users(session)
            selected = {a.user_id for a in project.assignments if a.category == category}
            await query.edit_message_reply_markup(
                user_toggle_keyboard(users, selected, f"pj:{project.id}:asg:{category.value}", f"pj:{project.id}:assign")
            )

        elif action == "meta":
            await query.answer()
            await safe_edit(update, _metadata_text(project), metadata_field_keyboard(project, METADATA_FIELDS))

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
            if args[0] == "done":
                await query.answer()
                await _show_menu(update, context, session, project)
                return
            project_service.toggle_declaration(session, project, AssetCategory(args[0]))
            session.commit()
            created: list[str] = []
            toast, alert = "Saved", False
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
            await query.answer(toast, show_alert=alert)
            await query.edit_message_reply_markup(declaration_keyboard(project, f"pj:{project.id}:dt", done_text="◀️ Back to project"))

        elif action == "group":
            await query.answer()
            groups = group_service.list_active_groups(session)
            if not groups:
                await safe_edit(update, "No MG Group is authorised yet. Run /creategroup first.", back_to_project_keyboard(project.id))
                return
            await safe_edit(update, "💬 Which MG Group should receive this project's announcements?", group_choice_keyboard(groups, f"pj:{project.id}:grp"))

        elif action == "grp":
            try:
                project_service.set_group(session, project, None if args[0] == "none" else int(args[0]))
            except ProjectError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            session.commit()
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
            await query.answer("Archived ✅")
            verifier = user_by_id(session, actor.telegram_id)
            await post_to_group(context, project.mg_group_chat_id, notifications.archived_message(project, verifier))
            await _show_menu(update, context, session, project, prefix="✅ <b>Archived.</b>\n\n")
            log.info("Project %s archived by %s", project.name, actor.telegram_id)

        elif action == "reopen":
            async with project_lock(context, project.id):
                session.refresh(project)
                try:
                    project_service.reopen_project(session, project)
                except ProjectError as exc:
                    await query.answer(str(exc), show_alert=True)
                    return
                session.commit()
            await query.answer("Reopened")
            await post_to_group(context, project.mg_group_chat_id, notifications.reopened_message(project))
            await _show_menu(update, context, session, project, prefix="🔓 <b>Reopened.</b>\n\n")

        else:
            await query.answer("Unknown action", show_alert=True)


def status_label(project: Project) -> str:
    return STATUS_LABELS[project.status]
