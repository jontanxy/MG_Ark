from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..constants import (
    CATEGORY_FLAGS,
    CATEGORY_LABELS,
    METADATA_FIELDS,
    AssetCategory,
    FolderSpec,
    ProjectStatus,
    build_folder_tree,
)
from ..models import Assignment, Collection, Project, ProjectFolder, Tag, User
from ..util import normalise_terms, utcnow
from .drive import DriveClient, DriveError, folder_link

log = logging.getLogger(__name__)


class ProjectError(Exception):
    pass


# ----------------------------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------------------------


def get_project(session: Session, project_id: int) -> Project | None:
    return session.get(Project, project_id)


def list_projects(
    session: Session,
    statuses: Iterable[ProjectStatus] | None = None,
    group_chat_id: int | None = None,
) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc())
    if statuses is not None:
        stmt = stmt.where(Project.status.in_(list(statuses)))
    if group_chat_id is not None:
        stmt = stmt.where(Project.mg_group_chat_id == group_chat_id)
    return list(session.scalars(stmt).all())


def name_in_use(session: Session, name: str, exclude_id: int | None = None, collection_id: int | None = None) -> bool:
    """Project names must be unique inside their collection (or at the top level)."""
    stmt = select(Project.id).where(
        func.lower(Project.name) == name.strip().lower(),
        Project.status != ProjectStatus.DRAFT,
        Project.collection_id.is_(None) if collection_id is None else Project.collection_id == collection_id,
    )
    if exclude_id is not None:
        stmt = stmt.where(Project.id != exclude_id)
    return session.scalar(stmt) is not None


def stale_drafts(session: Session, user_id: int) -> list[Project]:
    return list(
        session.scalars(
            select(Project).where(Project.status == ProjectStatus.DRAFT, Project.created_by == user_id)
        ).all()
    )


# ----------------------------------------------------------------------------------------
# Creation & declarations
# ----------------------------------------------------------------------------------------


def validate_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not 2 <= len(cleaned) <= 100:
        raise ProjectError("Project name must be between 2 and 100 characters.")
    if any(ch in cleaned for ch in "<>"):
        raise ProjectError("Project name cannot contain < or >.")
    return cleaned


def create_draft(
    session: Session,
    name: str,
    created_by: int,
    creator_name: str,
    year: int,
    collection: Collection | None = None,
) -> Project:
    cleaned = validate_name(name)
    if name_in_use(session, cleaned, collection_id=collection.id if collection else None):
        where = f" in “{collection.name}”" if collection else ""
        raise ProjectError(f"A project named “{cleaned}” already exists{where}.")
    for draft in stale_drafts(session, created_by):
        session.delete(draft)
    project = Project(
        name=cleaned,
        status=ProjectStatus.DRAFT,
        created_by=created_by,
        creator=creator_name or "",
        year=year,
        collection_id=collection.id if collection else None,
        collection=collection.name if collection else "",
    )
    session.add(project)
    session.flush()
    return project


def delete_draft(session: Session, project: Project) -> None:
    if project.status != ProjectStatus.DRAFT:
        raise ProjectError("Only drafts can be discarded.")
    session.delete(project)
    session.flush()


def derive_asset_types(project: Project) -> str:
    parts = ["Working Files"]
    for category, flag in CATEGORY_FLAGS.items():
        if project.flag(flag):
            parts.append(CATEGORY_LABELS[category])
    return ", ".join(parts)


def set_declaration(session: Session, project: Project, category: AssetCategory, value: bool) -> None:
    flag = CATEGORY_FLAGS.get(category)
    if flag is None:
        raise ProjectError("That category cannot be declared.")
    setattr(project, flag, value)
    project.asset_types = derive_asset_types(project)
    session.flush()


def toggle_declaration(session: Session, project: Project, category: AssetCategory) -> bool:
    flag = CATEGORY_FLAGS[category]
    new_value = not bool(getattr(project, flag))
    set_declaration(session, project, category, new_value)
    return new_value


def declared_categories(project: Project) -> list[AssetCategory]:
    return [c for c, flag in CATEGORY_FLAGS.items() if project.flag(flag)]


# ----------------------------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------------------------


def set_tags(session: Session, project: Project, raw: str) -> list[str]:
    names = normalise_terms(raw)
    tags: list[Tag] = []
    for name in names:
        tag = session.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            session.flush()
        tags.append(tag)
    project.tags = tags
    session.flush()
    return names


def set_metadata_field(session: Session, project: Project, field: str, raw: str) -> str:
    """Set one editable field from user text. Returns the stored display value."""
    if field not in METADATA_FIELDS:
        raise ProjectError("Unknown field.")
    if field == "collection" and project.collection_id is not None:
        raise ProjectError("This project lives in a collection folder; its collection name comes from that folder.")
    value = raw.strip()
    if value in {"-", "—", "none", "clear"}:
        value = ""
    if field == "tags":
        return ", ".join(set_tags(session, project, value))
    if field == "year":
        if not value:
            project.year = None
        else:
            if not value.isdigit() or not 1990 <= int(value) <= 2100:
                raise ProjectError("Year must be a number between 1990 and 2100.")
            project.year = int(value)
        session.flush()
        return str(project.year or "")
    if field == "description":
        value = value[:2000]
    else:
        value = " ".join(value.split())[:200]
    setattr(project, field, value)
    session.flush()
    return value


# ----------------------------------------------------------------------------------------
# Assignments
# ----------------------------------------------------------------------------------------


def toggle_assignment(session: Session, project: Project, user_id: int, category: AssetCategory) -> bool:
    for a in list(project.assignments):
        if a.user_id == user_id and a.category == category:
            session.delete(a)
            project.assignments.remove(a)
            session.flush()
            return False
    if session.get(User, user_id) is None:
        raise ProjectError("User not found.")
    a = Assignment(project_id=project.id, user_id=user_id, category=category)
    session.add(a)
    project.assignments.append(a)
    session.flush()
    return True


def assignees_for(project: Project, category: AssetCategory | None) -> list[User]:
    """Users responsible for *category* (explicit or via ALL). ``None`` returns everyone assigned."""
    seen: dict[int, User] = {}
    for a in project.assignments:
        if category is None or a.category == AssetCategory.ALL or a.category == category:
            seen.setdefault(a.user_id, a.user)
    return sorted(seen.values(), key=lambda u: u.display_name.lower())


# ----------------------------------------------------------------------------------------
# Drive provisioning
# ----------------------------------------------------------------------------------------


def required_leaves(project: Project, tree: list[FolderSpec]) -> list[FolderSpec]:
    return [spec for spec in tree if spec.is_leaf and project.flag(spec.required_when)]


def _unique_root_name(drive: DriveClient, name: str, parent_id: str) -> str:
    candidate = name
    for n in range(2, 50):
        if drive.find_child_folder(candidate, parent_id) is None:
            return candidate
        candidate = f"{name} ({n})"
    raise ProjectError("Too many folders with that name already exist on Drive.")


def _record_folder(session: Session, project: Project, key: str, name: str, drive_id: str) -> ProjectFolder:
    row = project.folder(key)
    if row is None:
        row = ProjectFolder(project_id=project.id, key=key, name=name, drive_id=drive_id, link=folder_link(drive_id))
        session.add(row)
        project.folders.append(row)
    else:
        row.name, row.drive_id, row.link = name, drive_id, folder_link(drive_id)
    return row


async def provision_folders(session: Session, project: Project, drive: DriveClient, settings: Settings) -> Project:
    """Create the full archive tree on Drive and activate the project."""
    if project.status != ProjectStatus.DRAFT:
        raise ProjectError("Project folders have already been created.")
    if not settings.drive_root_folder_id and settings.google_auth_mode != "fake":
        raise ProjectError("DRIVE_ROOT_FOLDER_ID is not configured.")
    root_parent = settings.drive_root_folder_id or getattr(drive, "ROOT_ID", "root")
    if project.collection_folder is not None:
        from .collections import ensure_collection_folder  # local import: collections depends on projects

        collection = await ensure_collection_folder(session, project.collection_folder, drive, settings)
        root_parent = collection.drive_id
    tree = build_folder_tree(settings.folder_names())
    flags = {spec.key: (spec.create_when is None or project.flag(spec.create_when)) for spec in tree}
    project_name = project.name

    def _create() -> dict[str, tuple[str, str]]:
        created: dict[str, tuple[str, str]] = {}
        root_name = _unique_root_name(drive, project_name, root_parent)
        root = drive.create_folder(root_name, root_parent)
        created["root"] = (root_name, root.id)
        for spec in tree:
            if not flags[spec.key]:
                continue
            parent_id = created[spec.parent_key or "root"][1]
            folder = drive.create_folder(spec.name, parent_id)
            created[spec.key] = (spec.name, folder.id)
        return created

    created = await asyncio.to_thread(_create)
    for key, (name, drive_id) in created.items():
        _record_folder(session, project, key, name, drive_id)
    project.drive_root_id = created["root"][1]
    project.drive_link = folder_link(project.drive_root_id)
    project.status = ProjectStatus.ACTIVE
    project.activated_at = utcnow()
    project.asset_types = project.asset_types or derive_asset_types(project)
    session.flush()
    return project


async def ensure_folders(session: Session, project: Project, drive: DriveClient, settings: Settings) -> list[str]:
    """Create any folders missing from the tree (e.g. PSD enabled after creation). Returns created keys."""
    if not project.drive_root_id:
        raise ProjectError("Project has no Drive folder yet.")
    tree = build_folder_tree(settings.folder_names())
    missing = [
        spec
        for spec in tree
        if (spec.create_when is None or project.flag(spec.create_when)) and project.folder(spec.key) is None
    ]
    if not missing:
        return []
    known = {"root": project.drive_root_id, **{f.key: f.drive_id for f in project.folders}}

    def _create() -> dict[str, str]:
        made: dict[str, str] = {}
        for spec in missing:
            parent_id = known.get(spec.parent_key or "root") or made.get(spec.parent_key or "root")
            if parent_id is None:
                continue
            existing = drive.find_child_folder(spec.name, parent_id)
            folder = existing or drive.create_folder(spec.name, parent_id)
            made[spec.key] = folder.id
            known[spec.key] = folder.id
        return made

    made = await asyncio.to_thread(_create)
    for spec in missing:
        if spec.key in made:
            _record_folder(session, project, spec.key, spec.name, made[spec.key])
    session.flush()
    return list(made)


# ----------------------------------------------------------------------------------------
# State transitions
# ----------------------------------------------------------------------------------------


def apply_validation_outcome(session: Session, project: Project, complete: bool) -> tuple[ProjectStatus, ProjectStatus]:
    old = project.status
    project.last_validated_at = utcnow()
    project.last_complete = complete
    if old in (ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION):
        project.status = ProjectStatus.READY_FOR_VERIFICATION if complete else ProjectStatus.INCOMPLETE
    session.flush()
    return old, project.status


def verify_project(session: Session, project: Project, verified_by: int) -> Project:
    if project.status != ProjectStatus.READY_FOR_VERIFICATION or not project.last_complete:
        raise ProjectError("Project is not ready for verification — run a progress check first.")
    project.status = ProjectStatus.ARCHIVED
    project.verified_by = verified_by
    project.verified_at = utcnow()
    session.flush()
    return project


def reopen_project(session: Session, project: Project) -> Project:
    if project.status != ProjectStatus.ARCHIVED:
        raise ProjectError("Only archived projects can be reopened.")
    project.status = ProjectStatus.ACTIVE
    project.verified_by = None
    project.verified_at = None
    project.last_complete = False
    session.flush()
    return project


def set_group(session: Session, project: Project, chat_id: int | None) -> None:
    from .groups import is_group_authorised  # local import: groups is independent of projects

    if chat_id is not None and not is_group_authorised(session, chat_id):
        raise ProjectError("That chat is not an authorised MG Group.")
    project.mg_group_chat_id = chat_id
    session.flush()


def mark_reminded(session: Session, project: Project) -> None:
    project.last_reminder_at = utcnow()
    session.flush()


def _is_drive_error(exc: BaseException) -> bool:  # pragma: no cover - helper for handlers
    return isinstance(exc, DriveError)
