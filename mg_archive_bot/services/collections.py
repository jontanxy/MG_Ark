"""Collections: folders under the archive root that hold several sub-projects (e.g. BF/Opening, BF/Worship)."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Collection
from .drive import DriveClient, folder_link
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


async def ensure_collection_folder(session: Session, collection: Collection, drive: DriveClient, settings: Settings) -> Collection:
    """Make sure the collection's folder exists under the archive root (re-using a same-named folder if present)."""
    if collection.drive_id:
        return collection
    root_parent = settings.drive_root_folder_id or getattr(drive, "ROOT_ID", "root")
    if not root_parent:
        raise ProjectError("DRIVE_ROOT_FOLDER_ID is not configured.")
    name = collection.name

    def _find_or_create():
        found = drive.find_child_folder(name, root_parent)
        return found or drive.create_folder(name, root_parent)

    folder = await asyncio.to_thread(_find_or_create)
    collection.drive_id = folder.id
    collection.link = folder_link(folder.id)
    session.flush()
    return collection
