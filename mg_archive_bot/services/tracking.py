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
    "ID", "Collection", "Project", "Status", "Lead", "Year", "Event", "Ministry", "Style", "Colours", "Tags", "Asset types",
    "Timeline", "Contin Videos", "Contin Lyrics", "Titlebars", "PSD", "Assigned", "Created by", "Created", "Archived",
    "Verified by", "MG Group", "Previews", "Last checked", "Drive link", "Description",
]
DEFAULT_GRID_COLUMNS = 26  # a new Google spreadsheet is 26 columns wide (A..Z)


def column_letter(index: int) -> str:
    """A1-notation column name for a 1-based index: 1 -> A, 26 -> Z, 27 -> AA, 703 -> AAA."""
    if index < 1:
        raise ValueError("column index starts at 1")
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


LAST_COL = column_letter(len(HEADERS))  # "AA"

# The columns that the newest layout added. An index that still has the layout of the previous version is
# upgraded by INSERTING these columns into the sheet: Google then moves everything else along with the data
# (a column somebody added to the right, saved filter views, formats). Any other header is simply overwritten.
ADDED_COLUMNS = ("Titlebars",)
PREVIOUS_HEADERS = [h for h in HEADERS if h not in ADDED_COLUMNS]
PENDING_HEADERS = ["" if h in ADDED_COLUMNS else h for h in HEADERS]  # columns inserted, table not rewritten yet
if HEADERS[-1] in ADDED_COLUMNS:  # a blank last header is dropped by reads: "pending" would look like "previous"
    raise RuntimeError("A new index column must not be the last one")

CURRENT, PREVIOUS, PENDING, OTHER = "current", "previous", "pending", "other"


def layout_of(header: list[str]) -> str:
    """Which layout a header row belongs to (see :func:`rebuild` for what happens to each)."""
    if header == HEADERS:
        return CURRENT
    if header[: len(PREVIOUS_HEADERS)] == PREVIOUS_HEADERS:
        return PREVIOUS
    if header == PENDING_HEADERS:
        return PENDING
    return OTHER


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
        _user_name(session, project.lead_id),
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
        _yes(project.has_titlebars),
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


def _structure_requests(sheet_id: int, *, rename: bool) -> list[dict]:
    """Frozen bold header row and a filter across every column (plus the tab name on first use)."""
    properties: dict = {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}
    fields = "gridProperties.frozenRowCount"
    if rename:
        properties["title"] = SHEET_TITLE
        fields = f"title,{fields}"
    return [
        {"updateSheetProperties": {"properties": properties, "fields": fields}},
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
    ]


def _resize_requests(sheet_id: int) -> list[dict]:
    return [{"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(HEADERS)}}}]


def _format_requests(sheet_id: int) -> list[dict]:
    return _structure_requests(sheet_id, rename=True) + _resize_requests(sheet_id)


def _widen_requests(sheet_id: int, columns: int) -> list[dict]:
    """Columns to add until every header fits: Google refuses to read or write a range beyond the sheet's grid."""
    if columns >= len(HEADERS):
        return []
    return [{"appendDimension": {"sheetId": sheet_id, "dimension": "COLUMNS", "length": len(HEADERS) - columns}}]


def _insert_requests(sheet_id: int) -> list[dict]:
    """Make room for the added columns inside a sheet of the previous layout (left to right, final positions)."""
    return [
        {
            "insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1},
                "inheritFromBefore": index > 0,
            }
        }
        for index, header in enumerate(HEADERS)
        if header in ADDED_COLUMNS
    ]


def create_sheet(drive: DriveClient, sheets: SheetsClient, settings: Settings) -> tuple[str, str]:
    """Blocking, no database access: create the spreadsheet in the archive root and format its header."""
    root_parent = settings.drive_root_folder_id or getattr(drive, "ROOT_ID", "root")
    created = drive.create_spreadsheet(settings.tracking_sheet_title, root_parent)
    grids = sheets.sheet_grids(created.id)
    first_title, (first_sheet_id, columns) = next(iter(grids.items()), ("Sheet1", (0, DEFAULT_GRID_COLUMNS)))
    sheets.batch_update(created.id, _widen_requests(first_sheet_id, columns))
    sheets.update_values(created.id, f"'{first_title}'!A1:{LAST_COL}1", [HEADERS])
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


def _prepare_sheet(sheets: SheetsClient, spreadsheet_id: str) -> tuple[int, int, str]:
    """Find the index tab and read its header. Returns (sheet id, columns in its grid, layout of the header).

    No cell is written here. A header that is not the current layout is only ever replaced by :func:`rebuild`,
    together with every row, so that the header can be trusted: rows are never left in one layout under the
    header of another.
    """
    grids = sheets.sheet_grids(spreadsheet_id)
    if SHEET_TITLE in grids:
        sheet_id, columns = grids[SHEET_TITLE]
    else:  # a spreadsheet supplied through TRACKING_SHEET_ID: its first tab becomes the index
        _, (sheet_id, columns) = next(iter(grids.items()), ("Sheet1", (0, DEFAULT_GRID_COLUMNS)))
        rename = {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "title": SHEET_TITLE}, "fields": "title"}}
        sheets.batch_update(spreadsheet_id, [rename])
    readable = column_letter(max(1, min(columns, len(HEADERS))))  # never ask for cells beyond the grid
    header = sheets.get_values(spreadsheet_id, _range(f"A1:{readable}1"))
    return sheet_id, columns, layout_of(header[0] if header else [])


def upsert_row(sheets: SheetsClient, spreadsheet_id: str, row: list[str]) -> str:
    """Blocking. Update the row whose ID (column A) matches, else append.

    Returns 'updated' | 'appended', or 'needs_rebuild' when the column layout is not current: nothing was
    written and the caller must run :func:`rebuild`.
    """
    _, _, layout = _prepare_sheet(sheets, spreadsheet_id)
    if layout != CURRENT:
        return "needs_rebuild"
    ids = sheets.get_values(spreadsheet_id, _range("A:A"))
    for index, cells in enumerate(ids, start=1):
        if index == 1:
            continue
        if cells and cells[0] == row[0]:
            sheets.update_values(spreadsheet_id, _range(f"A{index}:{LAST_COL}{index}"), [row])
            return "updated"
    sheets.append_values(spreadsheet_id, _range(f"A:{LAST_COL}"), [row])
    return "appended"


def delete_row(sheets: SheetsClient, spreadsheet_id: str, project_id: int) -> str:
    """Blocking. Remove the row whose ID matches (rows below move up).

    Returns 'deleted' | 'absent', or 'needs_rebuild' when the column layout is not current: nothing was removed
    and the caller must run :func:`rebuild` (which leaves revoked projects out anyway).
    """
    sheet_id, _, layout = _prepare_sheet(sheets, spreadsheet_id)
    if layout != CURRENT:
        return "needs_rebuild"
    ids = sheets.get_values(spreadsheet_id, _range("A:A"))
    for index, cells in enumerate(ids, start=1):
        if index > 1 and cells and cells[0] == str(project_id):
            sheets.batch_update(
                spreadsheet_id,
                [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": index - 1, "endIndex": index}}}],
            )
            return "deleted"
    return "absent"


def all_rows(session: Session, tz: tzinfo) -> list[list[str]]:
    """Every project that belongs in the index: drafts and revoked (cancelled) projects are left out."""
    projects = session.scalars(
        select(Project).where(Project.status.not_in([ProjectStatus.DRAFT, ProjectStatus.CANCELLED])).order_by(Project.id)
    ).all()
    return [build_row(session, p, tz) for p in projects]


def rebuild(sheets: SheetsClient, spreadsheet_id: str, rows: list[list[str]]) -> int:
    """Blocking. Rewrite the whole sheet (header + every project). Returns the number of project rows.

    The header, every row and the blanks that wipe left-over rows are written by ONE request, so the table is
    never half written and never empty: it is the old one until that request succeeds. Whatever fails before it
    leaves a header that is not the current layout, and the next sync rebuilds again.

    What happens first depends on the header that is found:

    * current: nothing. The filter and the column widths are the user's to change.
    * previous version: the new columns are inserted where they belong, which moves the rest of the sheet
      (including a user's own columns, filter views and formats) one step to the right.
    * pending: those columns were inserted by an earlier attempt that did not get to write; nothing more to do.
    * anything else (empty, unknown): the grid is widened and the header row and filter are set up again.
    """
    sheet_id, columns, layout = _prepare_sheet(sheets, spreadsheet_id)
    if layout == PREVIOUS:
        # One attempt only: repeating an insert that did arrive would add the column twice. After a failure the
        # next sync reads the header again and sees whether the column is there ("pending") or not ("previous").
        sheets.batch_update(spreadsheet_id, _insert_requests(sheet_id), retry=False)
    elif layout == OTHER:
        sheets.batch_update(spreadsheet_id, _widen_requests(sheet_id, columns) + _structure_requests(sheet_id, rename=False))
    existing = len(sheets.get_values(spreadsheet_id, _range(f"A:{LAST_COL}")))
    blank = [""] * len(HEADERS)
    table = [HEADERS, *rows, *([blank] * (existing - len(rows) - 1))]
    sheets.update_values(spreadsheet_id, _range(f"A1:{LAST_COL}{len(table)}"), table)
    if layout == OTHER:
        try:  # widths are fitted to the new content; cosmetic, the data above is already complete
            sheets.batch_update(spreadsheet_id, _resize_requests(sheet_id))
        except SheetsError as exc:
            log.warning("Index sheet rebuilt, but the columns could not be resized: %s", exc)
    return len(rows)


__all__ = ["HEADERS", "PREVIOUS_HEADERS", "LAST_COL", "column_letter", "layout_of", "SheetsError", "build_row", "create_sheet", "remember_sheet", "ensure_sheet", "upsert_row", "delete_row", "rebuild", "all_rows", "stored_sheet"]
