from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..constants import (
    PREVIEW_SOURCE_KEYS,
    VIDEO_EXTENSIONS,
    AssetCategory,
    ProjectStatus,
    build_folder_tree,
    folder_path_label,
)
from ..models import Project, ValidationRun
from ..util import utcnow
from .drive import DriveClient, DriveError, DriveFile
from .projects import apply_validation_outcome

log = logging.getLogger(__name__)

MAX_DEPTH = 3


@dataclass
class ValidationItem:
    key: str
    label: str
    category: str
    required: bool
    file_count: int | None
    ok: bool
    note: str = ""
    error: bool = False  # the folder could not be checked (Drive failure) — not the same as empty


@dataclass
class ValidationReport:
    project_id: int
    items: list[ValidationItem] = field(default_factory=list)
    complete: bool = False
    checked_at: str = ""

    @property
    def required_items(self) -> list[ValidationItem]:
        return [i for i in self.items if i.required]

    @property
    def missing(self) -> list[ValidationItem]:
        return [i for i in self.items if i.required and not i.ok and not i.error]

    @property
    def errored(self) -> list[ValidationItem]:
        return [i for i in self.items if i.required and i.error]

    @property
    def had_errors(self) -> bool:
        return bool(self.errored)

    def to_json(self) -> str:
        return json.dumps({"complete": self.complete, "checked_at": self.checked_at, "items": [asdict(i) for i in self.items]})

    @classmethod
    def from_json(cls, project_id: int, raw: str) -> ValidationReport:
        data = json.loads(raw or "{}")
        return cls(
            project_id=project_id,
            items=[ValidationItem(**i) for i in data.get("items", [])],
            complete=bool(data.get("complete")),
            checked_at=data.get("checked_at", ""),
        )


@dataclass
class ScanResult:
    report: ValidationReport
    source_files: dict[str, list[DriveFile]] = field(default_factory=dict)
    old_status: ProjectStatus | None = None
    new_status: ProjectStatus | None = None

    @property
    def became_ready(self) -> bool:
        return self.new_status == ProjectStatus.READY_FOR_VERIFICATION and self.old_status != self.new_status


JUNK_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})


def is_junk(f: DriveFile) -> bool:
    """OS metadata files and empty files never count as an upload."""
    name = f.name.lower()
    return name.startswith((".", "._")) or name in JUNK_NAMES or f.size == 0


def is_video(f: DriveFile) -> bool:
    return (not f.is_folder) and (f.mime_type.startswith("video/") or f.extension in VIDEO_EXTENSIONS)


def collect_files(drive: DriveClient, folder_id: str, max_depth: int = MAX_DEPTH) -> list[DriveFile]:
    """Breadth-first listing of non-folder files under *folder_id* up to *max_depth* levels."""
    files: list[DriveFile] = []
    queue: deque[tuple[str, int]] = deque([(folder_id, 0)])
    while queue:
        fid, depth = queue.popleft()
        for child in drive.list_children(fid):
            if child.is_folder:
                if depth + 1 < max_depth:
                    queue.append((child.id, depth + 1))
            elif not is_junk(child):
                files.append(child)
    return files


def scan_project_sync(project_snapshot: dict, folder_ids: dict[str, str], drive: DriveClient, settings: Settings) -> tuple[list[ValidationItem], dict[str, list[DriveFile]]]:
    """Blocking Drive scan. *project_snapshot* holds the declaration flags; safe to run in a thread."""
    tree = build_folder_tree(settings.folder_names())
    items: list[ValidationItem] = []
    sources: dict[str, list[DriveFile]] = {}
    for spec in tree:
        if not spec.is_leaf:
            continue
        required = spec.required_when == "always" or bool(project_snapshot.get(spec.required_when or "", False))
        label = folder_path_label(tree, spec.key)
        category = (spec.category or AssetCategory.ALL).value
        if not required:
            items.append(ValidationItem(spec.key, label, category, False, None, True, "not declared"))
            continue
        folder_id = folder_ids.get(spec.key)
        if not folder_id:
            items.append(ValidationItem(spec.key, label, category, True, 0, False, "folder not provisioned"))
            continue
        try:
            files = collect_files(drive, folder_id)
        except DriveError as exc:
            log.warning("Drive scan failed for %s/%s: %s", project_snapshot.get("name"), spec.key, exc)
            items.append(ValidationItem(spec.key, label, category, True, None, False, f"could not check: {exc}", error=True))
            continue
        if spec.key in PREVIEW_SOURCE_KEYS:
            sources[spec.key] = [f for f in files if is_video(f)]
        items.append(ValidationItem(spec.key, label, category, True, len(files), len(files) > 0))
    return items, sources


async def validate_project(session: Session, project: Project, drive: DriveClient, settings: Settings) -> ScanResult:
    """Scan Drive, persist a ValidationRun, and apply the status transition."""
    snapshot = {
        "name": project.name,
        "has_timeline": project.has_timeline,
        "has_contin_videos": project.has_contin_videos,
        "has_contin_lyrics": project.has_contin_lyrics,
        "has_psd": project.has_psd,
    }
    folder_ids = {f.key: f.drive_id for f in project.folders}
    items, sources = await asyncio.to_thread(scan_project_sync, snapshot, folder_ids, drive, settings)
    report = ValidationReport(
        project_id=project.id,
        items=items,
        complete=all(i.ok for i in items if i.required),
        checked_at=utcnow().isoformat(timespec="seconds"),
    )
    if report.had_errors:
        # Drive could not be consulted for at least one required folder: this scan proves nothing.
        # Keep the previous status and report; never announce or remind from it.
        report.complete = False
        return ScanResult(report=report, source_files=sources, old_status=project.status, new_status=project.status)
    session.add(ValidationRun(project_id=project.id, complete=report.complete, report_json=report.to_json()))
    old, new = apply_validation_outcome(session, project, report.complete)
    return ScanResult(report=report, source_files=sources, old_status=old, new_status=new)


def latest_report(session: Session, project: Project) -> ValidationReport | None:
    run = session.scalar(
        select(ValidationRun).where(ValidationRun.project_id == project.id).order_by(ValidationRun.run_at.desc(), ValidationRun.id.desc())
    )
    if run is None:
        return None
    report = ValidationReport.from_json(project.id, run.report_json)
    report.checked_at = report.checked_at or run.run_at.isoformat(timespec="seconds")
    return report
