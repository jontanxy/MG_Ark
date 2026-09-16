"""The project index: a Google Sheet with one row per project, kept in sync by the bot."""
from __future__ import annotations

import logging
from datetime import tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..constants import STATUS_LABELS, ProjectStatus
from ..models import MGGroup, Project, Setting, User
from ..util import fmt_dt
from .drive import DriveClient
from .projects import assignees_for
from .sheets import SheetsClient, SheetsError, spreadsheet_url

log = logging.getLogger(__name__)

SHEET_TITLE = "Projects"
SHEET_ID_KEY = "tracking_sheet_id"
SHEET_URL_KEY = "tracking_sheet_url"

HEADERS = [
    "ID", "Collection", "Project", "Status", "Year", "Event", "Ministry", "Style", "Colours", "Tags", "Asset types",
    "Timeline", "Contin Videos", "Contin Lyrics", "PSD", "Assigned", "Created by", "Created", "Archived", "Verified by",
    "MG Group", "Previews", "Last checked", "Drive link", "Description",
]
LAST_COL = chr(ord("A") + len(HEADERS) - 1)  # "Y"


def _yes(value: bool) -> str:
    return "Yes" if value else "No"


def _user_name(session: Session, user_id: int | None) -> str:
    if user_id is None:
        return ""
    user = session.get(User, user_id)
    return user.display_name if user else str(user_id)


def build_row(session: Session, project: Project, tz: tzinfo) -> list[str]:
    group = session.get(MGGroup, project.mg_group_chat_id) if project.mg_group_chat_id else None
    status = STATUS_LABELS.get(project.status, project.status.value).split(" ", 1)[-1]
    return [
        str(project.id),
        project.collection_folder.name if project.collection_folder else (project.collection or ""),
        project.name,
        status,
        str(project.year or ""),
        project.event or "",
        project.ministry or "",
        project.style or "",
        project.colours or "",
        ", ".join(project.tag_names),
        project.asset_types or "",
        _yes(project.has_timeline),
        _yes(project.has_contin_videos),
        _yes(project.has_contin_lyrics),
        _yes(project.has_psd),
        ", ".join(u.display_name for u in assignees_for(project, None)),
        _user_name(session, project.created_by),
        fmt_dt(project.created_at, tz),
        fmt_dt(project.verified_at, tz) if project.status == ProjectStatus.ARCHIVED else "",
        _user_name(session, project.verified_by) if project.status == ProjectStatus.ARCHIVED else "",
        group.title if group else "",
        str(len(project.ready_previews)),
        fmt_dt(project.last_validated_at, tz) if project.last_validated_at else "",
        project.drive_link or "",
        project.description or "",
    ]


def stored_sheet(session: Session) -> tuple[str | None, str | None]:
    sid = session.get(Setting, SHEET_ID_KEY)
    url = session.get(Setting, SHEET_URL_KEY)
    return (sid.value if sid and sid.value else None), (url.value if url and url.value else None)


def _format_requests(sheet_id: int) -> list[dict]:
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "title": SHEET_TITLE, "gridProperties": {"frozenRowCount": 1}},
                "fields": "title,gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(HEADERS)}}},
    ]


def create_sheet(drive: DriveClient, sheets: SheetsClient, settings: Settings) -> tuple[str, str]:
    """Blocking, no database access: create the spreadsheet in the archive root and format its header."""
    root_parent = settings.drive_root_folder_id or getattr(drive, "ROOT_ID", "root")
    created = drive.create_spreadsheet(settings.tracking_sheet_title, root_parent)
    ids = sheets.sheet_ids(created.id)
    first_sheet_id = next(iter(ids.values()), 0)
    sheets.update_values(created.id, f"'{next(iter(ids), 'Sheet1')}'!A1:{LAST_COL}1", [HEADERS])
    sheets.batch_update(created.id, _format_requests(first_sheet_id))
    log.info("Created tracking sheet %s (%s)", settings.tracking_sheet_title, created.id)
    return created.id, spreadsheet_url(created.id)


def remember_sheet(session: Session, sheet_id: str, url: str) -> None:
    session.merge(Setting(key=SHEET_ID_KEY, value=sheet_id))
    session.merge(Setting(key=SHEET_URL_KEY, value=url))
    session.flush()


def ensure_sheet(session: Session, drive: DriveClient, sheets: SheetsClient, settings: Settings) -> tuple[str, str]:
    """Blocking convenience (tests / scripts): return (spreadsheet id, url), creating the sheet on first use.

    The bot itself uses :func:`create_sheet` in a thread and :func:`remember_sheet` on the event loop so that no
    database write is ever held across the network calls.
    """
    if settings.tracking_sheet_id:
        return settings.tracking_sheet_id, spreadsheet_url(settings.tracking_sheet_id)
    sheet_id, url = stored_sheet(session)
    if sheet_id:
        return sheet_id, url or spreadsheet_url(sheet_id)
    sheet_id, url = create_sheet(drive, sheets, settings)
    remember_sheet(session, sheet_id, url)
    return sheet_id, url


def _range(cells: str) -> str:
    return f"'{SHEET_TITLE}'!{cells}"


def _ensure_header(sheets: SheetsClient, spreadsheet_id: str) -> None:
    titles = sheets.sheet_ids(spreadsheet_id)
    if SHEET_TITLE not in titles:
        first_title, first_id = next(iter(titles.items()), ("Sheet1", 0))
        sheets.batch_update(spreadsheet_id, _format_requests(first_id))
        if not sheets.get_values(spreadsheet_id, _range("A1:A1")):
            sheets.update_values(spreadsheet_id, _range(f"A1:{LAST_COL}1"), [HEADERS])
        return
    first = sheets.get_values(spreadsheet_id, _range("A1:A1"))
    if not first or not first[0] or first[0][0] != HEADERS[0]:
        sheets.update_values(spreadsheet_id, _range(f"A1:{LAST_COL}1"), [HEADERS])


def upsert_row(sheets: SheetsClient, spreadsheet_id: str, row: list[str]) -> str:
    """Blocking. Update the row whose ID (column A) matches, else append. Returns 'updated' | 'appended'."""
    _ensure_header(sheets, spreadsheet_id)
    ids = sheets.get_values(spreadsheet_id, _range("A:A"))
    for index, cells in enumerate(ids, start=1):
        if index == 1:
            continue
        if cells and cells[0] == row[0]:
            sheets.update_values(spreadsheet_id, _range(f"A{index}:{LAST_COL}{index}"), [row])
            return "updated"
    sheets.append_values(spreadsheet_id, _range(f"A:{LAST_COL}"), [row])
    return "appended"


def delete_row(sheets: SheetsClient, spreadsheet_id: str, project_id: int) -> bool:
    """Blocking. Remove the row whose ID matches (rows below move up). Returns whether a row was removed."""
    _ensure_header(sheets, spreadsheet_id)
    ids = sheets.get_values(spreadsheet_id, _range("A:A"))
    for index, cells in enumerate(ids, start=1):
        if index > 1 and cells and cells[0] == str(project_id):
            sheet_id = sheets.sheet_ids(spreadsheet_id).get(SHEET_TITLE, 0)
            sheets.batch_update(
                spreadsheet_id,
                [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": index - 1, "endIndex": index}}}],
            )
            return True
    return False


def all_rows(session: Session, tz: tzinfo) -> list[list[str]]:
    """Every project that belongs in the index: drafts and revoked (cancelled) projects are left out."""
    projects = session.scalars(
        select(Project).where(Project.status.not_in([ProjectStatus.DRAFT, ProjectStatus.CANCELLED])).order_by(Project.id)
    ).all()
    return [build_row(session, p, tz) for p in projects]


def rebuild(sheets: SheetsClient, spreadsheet_id: str, rows: list[list[str]]) -> int:
    """Blocking. Rewrite the whole sheet (header + every project). Returns the number of project rows."""
    _ensure_header(sheets, spreadsheet_id)
    sheets.clear_values(spreadsheet_id, _range(f"A2:{LAST_COL}"))
    sheets.update_values(spreadsheet_id, _range(f"A1:{LAST_COL}1"), [HEADERS])
    if rows:
        sheets.update_values(spreadsheet_id, _range(f"A2:{LAST_COL}{len(rows) + 1}"), rows)
    return len(rows)


__all__ = ["HEADERS", "SheetsError", "build_row", "create_sheet", "remember_sheet", "ensure_sheet", "upsert_row", "delete_row", "rebuild", "all_rows", "stored_sheet"]
