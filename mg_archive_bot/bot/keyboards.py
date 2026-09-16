from __future__ import annotations

from collections.abc import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..constants import CATEGORY_FLAGS, CATEGORY_LABELS, REVOCABLE_STATUSES, AssetCategory, ProjectStatus, Role
from ..models import MGGroup, Project, User

DECLARABLE = (AssetCategory.TIMELINE, AssetCategory.CONTIN_VIDEOS, AssetCategory.CONTIN_LYRICS, AssetCategory.PSD)
ASSIGNABLE = (
    AssetCategory.ALL,
    AssetCategory.WORKING_FILE,
    AssetCategory.TIMELINE,
    AssetCategory.CONTIN_VIDEOS,
    AssetCategory.CONTIN_LYRICS,
    AssetCategory.PSD,
)
MAX_USER_BUTTONS = 60


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def collection_choice_keyboard(collections) -> InlineKeyboardMarkup:
    rows = [[_btn("📁 Top level (no collection)", "nw:col:none")]]
    for c in list(collections)[:MAX_USER_BUTTONS]:
        rows.append([_btn(f"📂 {c.name}", f"nw:col:{c.id}")])
    rows.append([_btn("➕ New collection…", "nw:col:new")])
    return InlineKeyboardMarkup(rows)


def declaration_keyboard(project: Project, prefix: str, done_text: str = "Continue ➡️") -> InlineKeyboardMarkup:
    rows = []
    for cat in DECLARABLE:
        on = project.flag(CATEGORY_FLAGS[cat])
        rows.append([_btn(f"{'✅' if on else '⬜️'} {CATEGORY_LABELS[cat]}", f"{prefix}:{cat.value}")])
    rows.append([_btn(done_text, f"{prefix}:done")])
    return InlineKeyboardMarkup(rows)


def group_choice_keyboard(groups: Iterable[MGGroup], prefix: str, *, allow_none: bool = True) -> InlineKeyboardMarkup:
    rows = [[_btn(f"💬 {g.title or g.chat_id}", f"{prefix}:{g.chat_id}")] for g in groups]
    if allow_none:
        rows.append([_btn("No group (announce later)", f"{prefix}:none")])
    return InlineKeyboardMarkup(rows)


def yes_skip_keyboard(yes_data: str, skip_data: str, yes_text: str = "Add metadata now", skip_text: str = "Skip") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(yes_text, yes_data), _btn(skip_text, skip_data)]])


def user_toggle_keyboard(users: Iterable[User], selected: set[int], prefix: str, done_data: str, done_text: str = "Done ✅") -> InlineKeyboardMarkup:
    rows = []
    for u in list(users)[:MAX_USER_BUTTONS]:
        mark = "✅" if u.telegram_id in selected else "⬜️"
        role = "★" if u.role != Role.DESIGNER else ""
        rows.append([_btn(f"{mark} {u.display_name}{role}", f"{prefix}:{u.telegram_id}")])
    rows.append([_btn(done_text, done_data)])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(confirm_data: str, cancel_data: str, confirm_text: str = "Create archive 🚀", cancel_text: str = "Cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(confirm_text, confirm_data), _btn(cancel_text, cancel_data)]])


def project_menu_keyboard(project: Project) -> InlineKeyboardMarkup:
    pid = project.id
    rows: list[list[InlineKeyboardButton]] = [
        [_btn("🔎 Check progress", f"pj:{pid}:check"), _btn("ℹ️ Details", f"pj:{pid}:details")],
        [_btn("📣 Announce", f"pj:{pid}:announce"), _btn("⏰ Remind", f"pj:{pid}:remind")],
        [_btn("👥 Assign designers", f"pj:{pid}:assign"), _btn("🏷 Edit metadata", f"pj:{pid}:meta")],
        [_btn("⚙️ Declared assets", f"pj:{pid}:decl"), _btn("💬 MG group", f"pj:{pid}:group")],
        [_btn("🎞 Generate previews", f"pj:{pid}:prev"), _btn("▶️ Previews", f"pj:{pid}:previews")],
    ]
    if project.status == ProjectStatus.CANCELLED:
        rows = [
            [_btn("ℹ️ Details", f"pj:{pid}:details"), _btn("♻️ Restore project", f"pj:{pid}:restore")],
            [_btn("◀️ Projects", "pl:0:open")],
        ]
        return InlineKeyboardMarkup(rows)
    if project.status == ProjectStatus.READY_FOR_VERIFICATION:
        rows.append([_btn("✅ Verify & archive", f"pj:{pid}:verify")])
    elif project.status == ProjectStatus.ARCHIVED:
        rows.append([_btn("🔓 Reopen", f"pj:{pid}:reopen")])
    if project.status in REVOCABLE_STATUSES:
        rows.append([_btn("🗑 Revoke project", f"pj:{pid}:revoke")])
    if project.drive_link:
        rows.append([InlineKeyboardButton("📁 Open in Google Drive", url=project.drive_link)])
    rows.append([_btn("◀️ Projects", "pl:0:open")])
    return InlineKeyboardMarkup(rows)


def confirm_revoke_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn("🗑 Yes, revoke and trash the folder", f"pj:{project_id}:revoke2"), _btn("Cancel", f"pj:{project_id}:menu")]]
    )


def back_to_project_keyboard(project_id: int, extra_rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(extra_rows or [])
    rows.append([_btn("◀️ Back to project", f"pj:{project_id}:menu")])
    return InlineKeyboardMarkup(rows)


def assign_category_keyboard(project: Project) -> InlineKeyboardMarkup:
    rows = []
    for cat in ASSIGNABLE:
        if cat in CATEGORY_FLAGS and not project.flag(CATEGORY_FLAGS[cat]):
            continue
        n = sum(1 for a in project.assignments if a.category == cat)
        rows.append([_btn(f"{CATEGORY_LABELS[cat]} ({n})", f"pj:{project.id}:asgcat:{cat.value}")])
    rows.append([_btn("◀️ Back to project", f"pj:{project.id}:menu")])
    return InlineKeyboardMarkup(rows)


def metadata_field_keyboard(project: Project, fields: dict[str, str]) -> InlineKeyboardMarkup:
    items = list(fields.items())
    rows = []
    for i in range(0, len(items), 2):
        rows.append([_btn(label, f"pj:{project.id}:mf:{key}") for key, label in items[i : i + 2]])
    rows.append([_btn("◀️ Back to project", f"pj:{project.id}:menu")])
    return InlineKeyboardMarkup(rows)


def projects_list_keyboard(projects: list[Project], page: int, page_size: int, mode: str) -> InlineKeyboardMarkup:
    start = page * page_size
    chunk = projects[start : start + page_size]
    from ..constants import STATUS_LABELS

    rows = [[_btn(f"{STATUS_LABELS[p.status].split(' ')[0]} {p.full_name}", f"pj:{p.id}:menu")] for p in chunk]
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ Prev", f"pl:{page - 1}:{mode}"))
    if start + page_size < len(projects):
        nav.append(_btn("Next ➡️", f"pl:{page + 1}:{mode}"))
    if nav:
        rows.append(nav)
    toggle = "open" if mode != "open" else "archived"
    rows.append([_btn("📦 Show archived & cancelled" if mode == "open" else "🟢 Show open", f"pl:0:{toggle}")])
    return InlineKeyboardMarkup(rows)


def search_card_keyboard(project: Project) -> InlineKeyboardMarkup:
    row = [_btn("▶️ Preview", f"sr:{project.id}:prev")]
    if project.drive_link:
        row.append(InlineKeyboardButton("📁 Open Archive", url=project.drive_link))
    row.append(_btn("ℹ️ Details", f"sr:{project.id}:details"))
    return InlineKeyboardMarkup([row])


def previews_keyboard(project: Project, back_data: str | None = None) -> InlineKeyboardMarkup:
    from ..util import human_size

    rows = []
    for p in project.ready_previews:
        size = f" ({human_size(p.size_bytes)})" if p.size_bytes else ""
        rows.append([InlineKeyboardButton(f"▶️ {p.preview_name or p.source_name}{size}", url=p.preview_link)])
    if back_data:
        rows.append([_btn("◀️ Back", back_data)])
    return InlineKeyboardMarkup(rows)


def more_results_keyboard(page: int, has_more: bool) -> InlineKeyboardMarkup | None:
    if not has_more:
        return None
    return InlineKeyboardMarkup([[_btn("More results ➡️", f"sp:{page + 1}")]])


def users_list_keyboard(users: list[User]) -> InlineKeyboardMarkup:
    from ..constants import ROLE_LABELS, UserStatus

    rows = []
    for u in users[:MAX_USER_BUTTONS]:
        flag = "🚫 " if u.status != UserStatus.ACTIVE else ""
        rows.append([_btn(f"{flag}{u.display_name} · {ROLE_LABELS[u.role]}", f"ad:u:{u.telegram_id}")])
    return InlineKeyboardMarkup(rows)


def user_card_keyboard(user: User, super_admin_id: int) -> InlineKeyboardMarkup:
    from ..constants import UserStatus

    rows: list[list[InlineKeyboardButton]] = []
    if user.role != Role.SUPER_ADMIN and user.telegram_id != super_admin_id:
        if user.role == Role.DESIGNER:
            rows.append([_btn("⬆️ Make Team Lead", f"ad:u:{user.telegram_id}:role:{Role.TEAM_LEAD.value}")])
        else:
            rows.append([_btn("⬇️ Make Designer", f"ad:u:{user.telegram_id}:role:{Role.DESIGNER.value}")])
        if user.status == UserStatus.ACTIVE:
            rows.append([_btn("🚫 Revoke access", f"ad:u:{user.telegram_id}:revoke")])
        else:
            rows.append([_btn("♻️ Restore access", f"ad:u:{user.telegram_id}:restore")])
    rows.append([_btn("◀️ All users", "ad:users")])
    return InlineKeyboardMarkup(rows)


def groups_list_keyboard(groups: list[MGGroup]) -> InlineKeyboardMarkup:
    from ..constants import GroupStatus

    rows = []
    for g in groups:
        if g.status == GroupStatus.ACTIVE:
            rows.append([_btn(f"🚫 Revoke “{g.title or g.chat_id}”", f"ad:g:{g.chat_id}:revoke")])
        else:
            rows.append([_btn(f"♻️ Restore “{g.title or g.chat_id}”", f"ad:g:{g.chat_id}:restore")])
    return InlineKeyboardMarkup(rows)


def confirm_revoke_group_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("Yes, revoke and leave", f"ad:g:{chat_id}:revoke2"), _btn("Cancel", "ad:groups")]])


def confirm_verify_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("✅ Yes, archive it", f"pj:{project_id}:verify2"), _btn("Cancel", f"pj:{project_id}:menu")]])
