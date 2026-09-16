"""Google Sheets access (blocking; call via ``asyncio.to_thread``) plus an in-memory fake."""
from __future__ import annotations

import logging
import threading
from typing import Protocol, runtime_checkable

from ..config import Settings
from .drive import DriveError, GoogleDriveClient, load_credentials

log = logging.getLogger(__name__)

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


def spreadsheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


class SheetsError(DriveError):
    """Raised for any Sheets failure the bot should report."""


@runtime_checkable
class SheetsClient(Protocol):
    def sheet_ids(self, spreadsheet_id: str) -> dict[str, int]: ...
    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[str]]: ...
    def update_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None: ...
    def append_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None: ...
    def clear_values(self, spreadsheet_id: str, range_: str) -> None: ...
    def batch_update(self, spreadsheet_id: str, requests: list[dict]) -> None: ...


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

    def _run(self, request):
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=3)
        except GoogleDriveClient._failure_types() as exc:  # pragma: no cover - network
            if isinstance(exc, HttpError) and exc.resp.status == 403 and b"accessNotConfigured" in (exc.content or b""):
                raise SheetsError(
                    "The Google Sheets API is not enabled for this Cloud project. Enable it at "
                    "https://console.cloud.google.com/apis/library/sheets.googleapis.com and try again."
                ) from exc
            raise SheetsError(f"Google Sheets error: {GoogleDriveClient._translate(exc)}") from exc

    def sheet_ids(self, spreadsheet_id: str) -> dict[str, int]:
        resp = self._run(self._service().spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties"))
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in resp.get("sheets", [])}

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

    def batch_update(self, spreadsheet_id: str, requests: list[dict]) -> None:
        if requests:
            self._run(self._service().spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}))


class InMemorySheetsClient:
    """Enough of the Sheets API for tests and GOOGLE_AUTH_MODE=fake: one grid per sheet title."""

    def __init__(self) -> None:
        self.books: dict[str, dict[str, list[list[str]]]] = {}
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def _book(self, spreadsheet_id: str) -> dict[str, list[list[str]]]:
        return self.books.setdefault(spreadsheet_id, {"Sheet1": []})

    @staticmethod
    def _split(range_: str) -> tuple[str, str]:
        title, _, cells = range_.partition("!")
        return title.strip("'"), cells

    @staticmethod
    def _row_index(cells: str) -> int | None:
        digits = "".join(ch for ch in cells.split(":")[0] if ch.isdigit())
        return int(digits) - 1 if digits else None

    def sheet_ids(self, spreadsheet_id: str) -> dict[str, int]:
        return {title: i for i, title in enumerate(self._book(spreadsheet_id))}

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[str]]:
        title, cells = self._split(range_)
        rows = self._book(spreadsheet_id).get(title, [])
        if cells.upper().startswith("A:A"):
            return [[r[0]] if r else [] for r in rows]
        return [list(r) for r in rows]

    def update_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None:
        title, cells = self._split(range_)
        with self._lock:
            rows = self._book(spreadsheet_id).setdefault(title, [])
            start = self._row_index(cells) or 0
            for offset, row in enumerate(values):
                while len(rows) <= start + offset:
                    rows.append([])
                rows[start + offset] = list(row)

    def append_values(self, spreadsheet_id: str, range_: str, values: list[list[str]]) -> None:
        title, _ = self._split(range_)
        with self._lock:
            self._book(spreadsheet_id).setdefault(title, []).extend(list(r) for r in values)

    def clear_values(self, spreadsheet_id: str, range_: str) -> None:
        title, _ = self._split(range_)
        with self._lock:
            self._book(spreadsheet_id)[title] = []

    def batch_update(self, spreadsheet_id: str, requests: list[dict]) -> None:
        self.requests.extend(requests)
        for req in requests:
            props = req.get("updateSheetProperties", {}).get("properties", {})
            if "title" in props:
                book = self._book(spreadsheet_id)
                old = next((t for t, i in self.sheet_ids(spreadsheet_id).items() if i == props.get("sheetId", 0)), None)
                if old is not None and old != props["title"]:
                    book[props["title"]] = book.pop(old)
            rng = req.get("deleteDimension", {}).get("range")
            if rng and rng.get("dimension") == "ROWS":
                title = next((t for t, i in self.sheet_ids(spreadsheet_id).items() if i == rng.get("sheetId", 0)), None)
                if title is not None:
                    with self._lock:
                        rows = self._book(spreadsheet_id)[title]
                        del rows[rng["startIndex"] : rng["endIndex"]]


def build_sheets_client(settings: Settings) -> SheetsClient:
    if settings.google_auth_mode == "fake":
        return InMemorySheetsClient()
    return GoogleSheetsClient(load_credentials(settings))
