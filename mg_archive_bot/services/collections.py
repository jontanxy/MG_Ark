"""Collections: folders under the archive root that hold several sub-projects (e.g. BF/Opening, BF/Worship)."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Collection, Project
from .drive import DriveClient, folder_link
from ..constants import ProjectStatus
from .projects import ProjectError, validate_name


def list_collections(session: Session) -> list[Collection]:
    return list(session.scalars(select(Collection).order_by(func.lower(Collection.name))).all())


def get_collection(session: Session, collection_id: int | None) -> Collection | None:
    return session.get(Collection, collection_id) if collection_id is not None else None


def find_by_name(session: Session, name: str) -> Collection | None:
    return session.scalar(select(Collection).where(func.lower(Collection.name) == name.strip().lower()))


def create_collection(session: Session, name: str, created_by: int) -> Collection:
    """Create a collection (or return the existing one with that name). The Drive folder is created lazily."""
    cleaned = validate_name(name)
    existing = find_by_name(session, cleaned)
    if existing is not None:
        return existing
    collection = Collection(name=cleaned, created_by=created_by)
    session.add(collection)
    session.flush()
    return collection


def taken_by_top_level_project(session: Session, name: str, *, except_project_id: int | None = None) -> bool:
    """Whether a project at the top level already carries this name (its Drive folder sits where the
    collection's folder would go). Drafts have no folder yet and do not count."""
    stmt = select(Project.id).where(
        func.lower(Project.name) == name.strip().lower(),
        Project.collection_id.is_(None),
        Project.status != ProjectStatus.DRAFT,
    )
    if except_project_id is not None:
        stmt = stmt.where(Project.id != except_project_id)
    return session.scalar(stmt.limit(1)) is not None


def name_taken_message(name: str) -> str:
    return f"A project at the top level is already called “{name}”. Choose another collection name, or rename that project first."


async def _members_with_folders(session: Session, collection: Collection, drive: DriveClient) -> int:
    """Projects whose Drive folders live inside the collection's folder. A cancelled project only counts while
    its own folder still exists (in the trash): once that is gone it can never be restored."""
    if collection.id is None:
        return 0
    rows = session.execute(
        select(Project.drive_root_id, Project.status).where(
            Project.collection_id == collection.id, Project.drive_root_id.is_not(None), Project.status != ProjectStatus.DRAFT
        )
    ).all()

    def _count() -> int:
        return sum(1 for root, status in rows if status != ProjectStatus.CANCELLED or drive.get_file(root) is not None)

    return await asyncio.to_thread(_count)


async def ensure_collection_folder(
    session: Session, collection: Collection, drive: DriveClient, settings: Settings, *, moving_root_id: str | None = None
) -> bool:
    """Make sure the collection has a usable folder under the archive root (``collection.drive_id``).

    Returns whether a folder was CREATED on Drive by this call (as opposed to found).

    * A folder that is already recorded is looked up on Drive first. If it is in the trash or gone, it is
      replaced when no project lives in the collection and refused otherwise (the projects' folders are in it).
    * A folder of the same name under the root is re-used, unless it is a project's own folder: a collection is
      never given a project's folder. *moving_root_id* is the folder of the project being moved, which may carry
      the collection's name because it is about to go inside the collection.
    * Earlier versions did re-use a project's folder of the same name. A collection recorded that way keeps
      working while projects live in it (they are inside that folder already); it gets a folder of its own once
      it is empty.
    """
    root_parent = settings.drive_root_folder_id or getattr(drive, "ROOT_ID", "root")
    if not root_parent:
        raise ProjectError("DRIVE_ROOT_FOLDER_ID is not configured.")
    name, known = collection.name, collection.drive_id
    project_roots = set(session.scalars(select(Project.drive_root_id).where(Project.drive_root_id.is_not(None))))
    if known:
        current = await asyncio.to_thread(drive.get_file, known)
        alive = current is not None and current.is_folder and not current.trashed
        if alive and known not in project_roots:
            return False
        members = await _members_with_folders(session, collection, drive)
        if members and alive:
            if known == moving_root_id:
                raise ProjectError(
                    f"The collection “{name}” uses this project's own Google Drive folder, and {members} project(s) are inside it. "
                    "Choose another collection."
                )
            return False  # recorded by an earlier version; its members already live inside it
        if members:
            problem = "is in the trash" if current is not None else "cannot be found (deleted, or no longer shared with the bot)"
            raise ProjectError(
                f"The Google Drive folder of the collection “{name}” {problem}, and {members} project(s) still belong to it. "
                "Put the folder back in Google Drive first."
            )
        collection.drive_id = collection.link = None  # nothing lives there any more: find or create one below

    def _find_or_create():
        taken_by_project = False
        for child in drive.list_children(root_parent):
            if not child.is_folder or child.name != name:
                continue
            if child.id not in project_roots:
                return child, False
            taken_by_project = taken_by_project or child.id != moving_root_id
        return (None, False) if taken_by_project else (drive.create_folder(name, root_parent), True)

    folder, created = await asyncio.to_thread(_find_or_create)
    if folder is None:
        raise ProjectError(name_taken_message(name))
    collection.drive_id = folder.id
    collection.link = folder_link(folder.id)
    session.flush()
    return created
