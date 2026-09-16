"""Google Drive access. All methods are *blocking*; call them via ``asyncio.to_thread``."""
from __future__ import annotations

import logging
import mimetypes
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Settings
from ..constants import FOLDER_MIME

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
DOWNLOAD_CHUNK = 32 * 1024 * 1024
UPLOAD_CHUNK = 16 * 1024 * 1024
NUM_RETRIES = 5


class DriveError(RuntimeError):
    """Raised for any Drive failure the bot should report to the user."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None = None
    md5: str | None = None
    modified_time: str | None = None
    parents: tuple[str, ...] = ()
    drive_id: str | None = None  # id of the Shared Drive holding the file, if any

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def fingerprint(self) -> str:
        """Identifies the file *content*; changes when the file is replaced or edited."""
        return self.md5 or f"{self.size}:{self.modified_time}"

    @property
    def extension(self) -> str:
        return Path(self.name).suffix.lower()


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


@runtime_checkable
class DriveClient(Protocol):
    def create_folder(self, name: str, parent_id: str) -> DriveFile: ...
    def find_child_folder(self, name: str, parent_id: str) -> DriveFile | None: ...
    def list_children(self, folder_id: str) -> list[DriveFile]: ...
    def get_file(self, file_id: str) -> DriveFile | None: ...
    def download(self, file_id: str, dest: Path, progress: Callable[[int, int | None], None] | None = None) -> Path: ...
    def upload(self, src: Path, parent_id: str, name: str, mime_type: str | None = None) -> DriveFile: ...
    def delete(self, file_id: str) -> None: ...


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# --------------------------------------------------------------------------------------
# Real client
# --------------------------------------------------------------------------------------


def load_credentials(settings: Settings):
    if settings.google_auth_mode == "service_account":
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(settings.google_service_account_file), scopes=DRIVE_SCOPES
        )
    if settings.google_auth_mode == "oauth":
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_file(str(settings.google_oauth_token_file), DRIVE_SCOPES)
    raise DriveError(f"unsupported GOOGLE_AUTH_MODE={settings.google_auth_mode}")


class GoogleDriveClient:
    """Thin, retrying wrapper around the Drive v3 API. Thread-safe via a thread-local service.

    Works whether the bot's Google identity is a member of the Shared Drive or merely has the root folder
    shared with it: listings use ``corpora='allDrives'`` and deletions move items to the trash (Content
    managers on a Shared Drive may trash but not permanently delete).
    """

    FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime,parents,driveId"

    def __init__(self, credentials) -> None:
        self._credentials = credentials
        self._local = threading.local()

    def _service(self):
        svc = getattr(self._local, "service", None)
        if svc is None:
            from googleapiclient.discovery import build

            svc = build("drive", "v3", credentials=self._credentials, cache_discovery=False)
            self._local.service = svc
        return svc

    @staticmethod
    def _list_kwargs() -> dict:
        # 'allDrives' returns everything the identity can see (My Drive, Shared Drive membership, or
        # folders shared directly with it); every query here is scoped to one parent so cost is negligible.
        return {"supportsAllDrives": True, "includeItemsFromAllDrives": True, "corpora": "allDrives"}

    @staticmethod
    def _to_file(item: dict) -> DriveFile:
        size = item.get("size")
        return DriveFile(
            id=item["id"],
            name=item.get("name", ""),
            mime_type=item.get("mimeType", ""),
            size=int(size) if size is not None else None,
            md5=item.get("md5Checksum"),
            modified_time=item.get("modifiedTime"),
            parents=tuple(item.get("parents") or ()),
            drive_id=item.get("driveId"),
        )

    @staticmethod
    def _translate(exc: BaseException) -> DriveError:
        """Map every Google client / transport failure onto DriveError so callers have one contract."""
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        if isinstance(exc, RefreshError):
            return DriveError(
                "Google credentials could not be refreshed. For OAuth mode re-run "
                "`python -m mg_archive_bot.tools.google_oauth` (tokens from a 'Testing' consent screen expire after 7 days); "
                f"for a service account check the key file. ({exc})"
            )
        if isinstance(exc, HttpError):
            return DriveError(f"Google Drive error: {exc}")
        return DriveError(f"Google Drive transport error: {exc!r}")

    @staticmethod
    def _failure_types() -> tuple[type[BaseException], ...]:
        import http.client

        import httplib2
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import Error as ApiClientError

        return (ApiClientError, GoogleAuthError, httplib2.HttpLib2Error, http.client.HTTPException, OSError)

    def _run(self, request):
        try:
            return request.execute(num_retries=NUM_RETRIES)
        except self._failure_types() as exc:  # pragma: no cover - network
            raise self._translate(exc) from exc

    def create_folder(self, name: str, parent_id: str) -> DriveFile:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        item = self._run(self._service().files().create(body=body, fields=self.FIELDS, supportsAllDrives=True))
        return self._to_file(item)

    def find_child_folder(self, name: str, parent_id: str) -> DriveFile | None:
        q = (
            f"'{_escape_query_value(parent_id)}' in parents and trashed = false "
            f"and mimeType = '{FOLDER_MIME}' and name = '{_escape_query_value(name)}'"
        )
        resp = self._run(
            self._service().files().list(q=q, fields=f"files({self.FIELDS})", pageSize=10, **self._list_kwargs())
        )
        files = resp.get("files") or []
        return self._to_file(files[0]) if files else None

    def list_children(self, folder_id: str) -> list[DriveFile]:
        q = f"'{_escape_query_value(folder_id)}' in parents and trashed = false"
        out: list[DriveFile] = []
        token: str | None = None
        while True:
            resp = self._run(
                self._service()
                .files()
                .list(
                    q=q,
                    fields=f"nextPageToken, files({self.FIELDS})",
                    pageSize=1000,
                    pageToken=token,
                    **self._list_kwargs(),
                )
            )
            out.extend(self._to_file(i) for i in resp.get("files") or [])
            token = resp.get("nextPageToken")
            if not token:
                return out

    def get_file(self, file_id: str) -> DriveFile | None:
        from googleapiclient.errors import HttpError

        try:
            item = self._run(self._service().files().get(fileId=file_id, fields=self.FIELDS, supportsAllDrives=True))
        except DriveError as exc:
            cause = exc.__cause__
            if isinstance(cause, HttpError) and cause.resp.status == 404:
                return None
            raise
        return self._to_file(item)

    def download(self, file_id: str, dest: Path, progress: Callable[[int, int | None], None] | None = None) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            request = self._service().files().get_media(fileId=file_id, supportsAllDrives=True)
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=DOWNLOAD_CHUNK)
                done = False
                while not done:
                    status, done = downloader.next_chunk(num_retries=NUM_RETRIES)
                    if progress and status:
                        progress(status.resumable_progress, status.total_size)
        except self._failure_types() as exc:  # pragma: no cover - network
            dest.unlink(missing_ok=True)
            raise DriveError(f"Download failed: {self._translate(exc)}") from exc
        return dest

    def upload(self, src: Path, parent_id: str, name: str, mime_type: str | None = None) -> DriveFile:
        from googleapiclient.http import MediaFileUpload

        mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            media = MediaFileUpload(str(src), mimetype=mime, resumable=True, chunksize=UPLOAD_CHUNK)
        except OSError as exc:
            raise DriveError(f"Upload failed: {exc}") from exc
        body = {"name": name, "parents": [parent_id]}
        item = self._run(
            self._service().files().create(body=body, media_body=media, fields=self.FIELDS, supportsAllDrives=True)
        )
        return self._to_file(item)

    def delete(self, file_id: str) -> None:
        """Move a file to the trash (works for Content managers on Shared Drives; auto-purged after 30 days)."""
        from googleapiclient.errors import HttpError

        try:
            self._run(
                self._service().files().update(fileId=file_id, body={"trashed": True}, fields="id", supportsAllDrives=True)
            )
        except DriveError as exc:
            cause = exc.__cause__
            if isinstance(cause, HttpError) and cause.resp.status == 404:
                return
            raise


# --------------------------------------------------------------------------------------
# In-memory fake (tests, and GOOGLE_AUTH_MODE=fake for trying the bot without Google)
# --------------------------------------------------------------------------------------


@dataclass
class _Node:
    file: DriveFile
    content: bytes = b""
    children: list[str] = field(default_factory=list)


class InMemoryDriveClient:
    ROOT_ID = "root-folder"

    def __init__(self) -> None:
        self._nodes: dict[str, _Node] = {}
        self._lock = threading.Lock()
        self._nodes[self.ROOT_ID] = _Node(DriveFile(self.ROOT_ID, "Archive Root", FOLDER_MIME))
        self.calls: list[tuple[str, tuple]] = []

    # -- helpers for tests -------------------------------------------------------------
    def put_file(
        self,
        parent_id: str,
        name: str,
        *,
        content: bytes = b"",
        size: int | None = None,
        md5: str | None = None,
        mime_type: str | None = None,
        modified_time: str = "2026-01-01T00:00:00.000Z",
    ) -> DriveFile:
        mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        f = DriveFile(
            id=f"file-{uuid.uuid4().hex[:8]}",
            name=name,
            mime_type=mime,
            size=size if size is not None else (len(content) or 1024),  # a test file is never 0 bytes unless asked
            md5=md5,
            modified_time=modified_time,
            parents=(parent_id,),
        )
        with self._lock:
            self._nodes[f.id] = _Node(f, content)
            self._nodes[parent_id].children.append(f.id)
        return f

    def replace_content(self, file_id: str, *, md5: str, modified_time: str) -> None:
        with self._lock:
            node = self._nodes[file_id]
            node.file = DriveFile(
                node.file.id, node.file.name, node.file.mime_type, node.file.size, md5, modified_time, node.file.parents
            )

    def all_files(self) -> Iterable[DriveFile]:
        return [n.file for n in self._nodes.values()]

    def path_of(self, file_id: str) -> str:
        parts = []
        cur = self._nodes.get(file_id)
        while cur is not None:
            parts.append(cur.file.name)
            cur = self._nodes.get(cur.file.parents[0]) if cur.file.parents else None
        return "/".join(reversed(parts))

    # -- DriveClient protocol ----------------------------------------------------------
    def create_folder(self, name: str, parent_id: str) -> DriveFile:
        self.calls.append(("create_folder", (name, parent_id)))
        with self._lock:
            if parent_id not in self._nodes:
                raise DriveError(f"parent not found: {parent_id}")
            f = DriveFile(f"folder-{uuid.uuid4().hex[:8]}", name, FOLDER_MIME, parents=(parent_id,))
            self._nodes[f.id] = _Node(f)
            self._nodes[parent_id].children.append(f.id)
        return f

    def find_child_folder(self, name: str, parent_id: str) -> DriveFile | None:
        for child in self.list_children(parent_id):
            if child.is_folder and child.name == name:
                return child
        return None

    def list_children(self, folder_id: str) -> list[DriveFile]:
        with self._lock:
            node = self._nodes.get(folder_id)
            if node is None:
                raise DriveError(f"folder not found: {folder_id}")
            return [self._nodes[c].file for c in node.children if c in self._nodes]

    def get_file(self, file_id: str) -> DriveFile | None:
        node = self._nodes.get(file_id)
        return node.file if node else None

    def download(self, file_id: str, dest: Path, progress=None) -> Path:
        node = self._nodes.get(file_id)
        if node is None:
            raise DriveError(f"file not found: {file_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(node.content)
        if progress:
            progress(len(node.content), len(node.content))
        return dest

    def upload(self, src: Path, parent_id: str, name: str, mime_type: str | None = None) -> DriveFile:
        data = Path(src).read_bytes()
        return self.put_file(parent_id, name, content=data, mime_type=mime_type)

    def delete(self, file_id: str) -> None:
        with self._lock:
            node = self._nodes.pop(file_id, None)
            if node:
                for parent in node.file.parents:
                    p = self._nodes.get(parent)
                    if p and file_id in p.children:
                        p.children.remove(file_id)


def build_drive_client(settings: Settings) -> DriveClient:
    if settings.google_auth_mode == "fake":
        log.warning("GOOGLE_AUTH_MODE=fake: using an in-memory Drive. Nothing is written to Google Drive!")
        return InMemoryDriveClient()
    return GoogleDriveClient(load_credentials(settings))
