"""Google Sheets access (blocking; call via ``asyncio.to_thread``) plus an in-memory fake."""
from __future__ import annotations

import copy
import logging
import re
import threading
from typing import Protocol, runtime_checkable

from ..config import Settings
from .drive import DriveError, GoogleDriveClient, load_credentials

log = logging.getLogger(__name__)

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
DEFAULT_GRID_COLUMNS = 26  # a new Google spreadsheet is 26 columns wide (A..Z)


def spreadsheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


class SheetsError(DriveError):
    """Raised for any Sheets failure the bot should report."""


@runtime_checkable
class SheetsClient(Protocol):
    def sheet_ids(self, spreadsheet_id: str) -> dict[str, int]: ...
    def sheet_grids(self, spreadsheet_id: str) -> dict[str, tuple[int, int]]: ...
    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[str]]: ...
    def update_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None: ...
    def append_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None: ...
    def clear_values(self, spreadsheet_id: str, range_: str) -> None: ...
    def batch_update(self, spreadsheet_id: str, requests: list[dict], *, retry: bool = True) -> None: ...


class GoogleSheetsClient:
    def __init__(self, credentials) -> None:
        self._credentials = credentials
        self._local = threading.local()

    def _service(self):
        svc = getattr(self._local, "service", None)
        if svc is None:
            from googleapiclient.discovery import build

            svc = build("sheets", "v4", credentials=self._credentials, cache_discovery=False)
            self._local.service = svc
        return svc

    def _run(self, request, retries: int = 3):
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=retries)
        except GoogleDriveClient._failure_types() as exc:  # pragma: no cover - network
            if isinstance(exc, HttpError) and exc.resp.status == 403 and b"accessNotConfigured" in (exc.content or b""):
                raise SheetsError(
                    "The Google Sheets API is not enabled for this Cloud project. Enable it at "
                    "https://console.cloud.google.com/apis/library/sheets.googleapis.com and try again."
                ) from exc
            raise SheetsError(f"Google Sheets error: {GoogleDriveClient._translate(exc)}") from exc

    def sheet_grids(self, spreadsheet_id: str) -> dict[str, tuple[int, int]]:
        """Sheet title -> (sheet id, number of columns in its grid)."""
        resp = self._run(self._service().spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties"))
        grids: dict[str, tuple[int, int]] = {}
        for sheet in resp.get("sheets", []):
            props = sheet["properties"]
            columns = int(props.get("gridProperties", {}).get("columnCount", DEFAULT_GRID_COLUMNS))
            grids[props["title"]] = (props["sheetId"], columns)
        return grids

    def sheet_ids(self, spreadsheet_id: str) -> dict[str, int]:
        return {title: sheet_id for title, (sheet_id, _) in self.sheet_grids(spreadsheet_id).items()}

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[str]]:
        resp = self._run(self._service().spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_))
        return resp.get("values", [])

    def update_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None:
        self._run(
            self._service()
            .spreadsheets()
            .values()
            .update(spreadsheetId=spreadsheet_id, range=range_, valueInputOption="RAW", body={"values": values})
        )

    def append_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None:
        self._run(
            self._service()
            .spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range_,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
        )

    def clear_values(self, spreadsheet_id: str, range_: str) -> None:
        self._run(self._service().spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=range_, body={}))

    def batch_update(self, spreadsheet_id: str, requests: list[dict], *, retry: bool = True) -> None:
        """Apply the requests as one all-or-nothing batch. ``retry=False`` is for requests that must not be
        repeated when the answer is lost (inserting a column twice would add two)."""
        if requests:
            request = self._service().spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
            self._run(request, retries=3 if retry else 0)


_A1 = re.compile(r"^([A-Za-z]+)(\d*)(?::([A-Za-z]+)(\d*))?$")


class InMemorySheetsClient:
    """Enough of the Sheets API for tests and GOOGLE_AUTH_MODE=fake.

    It follows Google where the bot depends on it: every tab starts 26 columns wide and refuses ranges, rows and
    requests that reach beyond its grid; ranges are honoured cell by cell, so whatever lies outside a range is
    left alone; reads drop empty trailing cells and rows; a batch is applied completely or not at all; a tab
    keeps its id when it is renamed.
    """

    def __init__(self) -> None:
        self.books: dict[str, dict[str, list[list[str]]]] = {}
        self.columns: dict[str, dict[str, int]] = {}
        self.ids: dict[str, dict[str, int]] = {}
        self.requests: list[dict] = []  # every batch request that was applied
        self.single_attempt: list[dict] = []  # those of them that were sent with retry=False
        self._lock = threading.RLock()

    # -- structure ------------------------------------------------------------------------------------------
    def _book(self, spreadsheet_id: str) -> dict[str, list[list[str]]]:
        return self.books.setdefault(spreadsheet_id, {"Sheet1": []})

    def _columns(self, spreadsheet_id: str, title: str) -> int:
        return self.columns.setdefault(spreadsheet_id, {}).get(title, DEFAULT_GRID_COLUMNS)

    def _id(self, spreadsheet_id: str, title: str) -> int:
        ids = self.ids.setdefault(spreadsheet_id, {})
        if title not in ids:
            ids[title] = max(ids.values(), default=-1) + 1
        return ids[title]

    def _title_of(self, spreadsheet_id: str, sheet_id: int) -> str:
        for title, known in self.sheet_ids(spreadsheet_id).items():
            if known == sheet_id:
                return title
        raise SheetsError(f"Google Sheets error: No grid with id: {sheet_id}")

    def sheet_ids(self, spreadsheet_id: str) -> dict[str, int]:
        with self._lock:
            return {title: self._id(spreadsheet_id, title) for title in self._book(spreadsheet_id)}

    def sheet_grids(self, spreadsheet_id: str) -> dict[str, tuple[int, int]]:
        with self._lock:
            return {title: (i, self._columns(spreadsheet_id, title)) for title, i in self.sheet_ids(spreadsheet_id).items()}

    # -- ranges ---------------------------------------------------------------------------------------------
    @staticmethod
    def _column_index(letters: str) -> int:
        index = 0
        for ch in letters.upper():
            index = index * 26 + (ord(ch) - ord("A") + 1)
        return index

    def _cells(self, spreadsheet_id: str, range_: str) -> tuple[str, int, int, int, int | None]:
        """(tab, first column, first row, column after the last, row after the last or None), all zero-based."""
        title, _, cells = range_.partition("!")
        title = title.strip("'")
        match = _A1.match(cells)
        if match is None:
            raise SheetsError(f"Google Sheets error: Unable to parse range: {range_}")
        first, first_row, last, last_row = match.groups()
        col0 = self._column_index(first) - 1
        row0 = int(first_row) - 1 if first_row else 0
        if last is None:
            col1, row1 = col0 + 1, (row0 + 1 if first_row else None)
        else:
            col1, row1 = self._column_index(last), (int(last_row) if last_row else None)
        limit = self._columns(spreadsheet_id, title)
        if col1 > limit:
            raise SheetsError(f"Google Sheets error: Range ({range_}) exceeds grid limits. Max columns: {limit}")
        return title, col0, row0, col1, row1

    def _fits(self, range_: str, values: list[list[str]], width: int, height: int | None) -> None:
        if any(len(row) > width for row in values) or (height is not None and len(values) > height):
            raise SheetsError(f"Google Sheets error: Requested writing within range [{range_}], but tried writing outside it")

    # -- values ---------------------------------------------------------------------------------------------
    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[str]]:
        with self._lock:
            title, col0, row0, col1, row1 = self._cells(spreadsheet_id, range_)
            found = [list(row[col0:col1]) for row in self._book(spreadsheet_id).get(title, [])[row0:row1]]
        for row in found:
            while row and row[-1] == "":
                row.pop()
        while found and not found[-1]:
            found.pop()
        return found

    @staticmethod
    def _write(rows: list[list[str]], at: int, col0: int, cells: list[str]) -> None:
        while len(rows) <= at:
            rows.append([])
        row = rows[at]
        row.extend([""] * (col0 + len(cells) - len(row)))
        row[col0 : col0 + len(cells)] = cells

    def update_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None:
        with self._lock:
            title, col0, row0, col1, row1 = self._cells(spreadsheet_id, range_)
            self._fits(range_, values, col1 - col0, None if row1 is None else row1 - row0)
            rows = self._book(spreadsheet_id).setdefault(title, [])
            for offset, row in enumerate(values):
                self._write(rows, row0 + offset, col0, list(row))

    def append_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None:
        """New rows are inserted right below the last row of the table found in the range (INSERT_ROWS)."""
        with self._lock:
            title, col0, _, col1, _ = self._cells(spreadsheet_id, range_)
            self._fits(range_, values, col1 - col0, None)
            rows = self._book(spreadsheet_id).setdefault(title, [])
            end = max((i + 1 for i, row in enumerate(rows) if any(cell != "" for cell in row[col0:col1])), default=0)
            rows[end:end] = [[""] * col0 + list(row) for row in values]

    def clear_values(self, spreadsheet_id: str, range_: str) -> None:
        with self._lock:
            title, col0, row0, col1, row1 = self._cells(spreadsheet_id, range_)
            for row in self._book(spreadsheet_id).get(title, [])[row0:row1]:
                for index in range(col0, min(col1, len(row))):
                    row[index] = ""

    # -- batch ----------------------------------------------------------------------------------------------
    def batch_update(self, spreadsheet_id: str, requests: list[dict], *, retry: bool = True) -> None:
        if not requests:
            return
        with self._lock:
            self._book(spreadsheet_id)
            self.sheet_ids(spreadsheet_id)
            saved = copy.deepcopy((self.books[spreadsheet_id], self.columns.get(spreadsheet_id, {}), self.ids[spreadsheet_id]))
            try:
                for request in requests:
                    self._apply(spreadsheet_id, request)
            except Exception:
                self.books[spreadsheet_id], self.columns[spreadsheet_id], self.ids[spreadsheet_id] = saved
                raise
            self.requests.extend(requests)
            if not retry:
                self.single_attempt.extend(requests)

    def _apply(self, spreadsheet_id: str, request: dict) -> None:
        book = self._book(spreadsheet_id)
        widths = self.columns.setdefault(spreadsheet_id, {})
        kind, body = next(iter(request.items()))
        if kind == "appendDimension":
            title = self._title_of(spreadsheet_id, body.get("sheetId", 0))
            if body.get("dimension") == "COLUMNS":
                widths[title] = self._columns(spreadsheet_id, title) + int(body["length"])
        elif kind == "insertDimension":
            rng = body["range"]
            title = self._title_of(spreadsheet_id, rng.get("sheetId", 0))
            start, count = rng["startIndex"], rng["endIndex"] - rng["startIndex"]
            if rng.get("dimension") == "COLUMNS":
                if start > self._columns(spreadsheet_id, title) or (body.get("inheritFromBefore") and start == 0):
                    raise SheetsError(f"Google Sheets error: Invalid insertDimension at column {start} of '{title}'")
                for row in book[title]:
                    if len(row) > start:
                        row[start:start] = [""] * count
                widths[title] = self._columns(spreadsheet_id, title) + count
            else:
                book[title][start:start] = [[] for _ in range(count)]
        elif kind == "deleteDimension":
            rng = body["range"]
            title = self._title_of(spreadsheet_id, rng.get("sheetId", 0))
            if rng.get("dimension") == "ROWS":
                del book[title][rng["startIndex"] : rng["endIndex"]]
        elif kind == "updateSheetProperties":
            props = body.get("properties", {})
            old = self._title_of(spreadsheet_id, props.get("sheetId", 0))
            new = props.get("title", old)
            if new != old:
                if new in book:
                    raise SheetsError(f'Google Sheets error: A sheet with the name "{new}" already exists')
                book[new] = book.pop(old)
                self.ids[spreadsheet_id][new] = self.ids[spreadsheet_id].pop(old)
                if old in widths:
                    widths[new] = widths.pop(old)
        elif kind in ("setBasicFilter", "autoResizeDimensions"):
            rng = body["filter"]["range"] if kind == "setBasicFilter" else body["dimensions"]
            title = self._title_of(spreadsheet_id, rng.get("sheetId", 0))
            reach = rng.get("endColumnIndex", rng.get("endIndex", 0))
            if reach > self._columns(spreadsheet_id, title):
                raise SheetsError(f"Google Sheets error: {kind} reaches column {reach}, beyond the grid of '{title}'")
        elif kind == "repeatCell":
            self._title_of(spreadsheet_id, body["range"].get("sheetId", 0))


def build_sheets_client(settings: Settings) -> SheetsClient:
    if settings.google_auth_mode == "fake":
        return InMemorySheetsClient()
    return GoogleSheetsClient(load_credentials(settings))
