"""Folder-by-folder listing of a project's Google Drive folder, rendered for Telegram."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import esc, human_size
from .drive import DriveClient, DriveError, file_link, folder_link
from .validation import is_junk

MAX_DEPTH = 6  # project folder → Working File → Fonts → up to three levels of designer sub-folders
CHUNK_LIMIT = 3500  # raw HTML characters per Telegram message (hard limit is 4096 after parsing)


@dataclass
class FileEntry:
    name: str
    size: int | None
    link: str


@dataclass
class FolderEntry:
    id: str
    name: str
    link: str
    key: str | None = None  # archive-tree key when this is one of the bot-created folders
    files: list[FileEntry] = field(default_factory=list)
    folders: list[FolderEntry] = field(default_factory=list)
    truncated: bool = False  # deeper levels were not read
    error: str = ""  # Drive refused to list this folder

    @property
    def total_files(self) -> int:
        return len(self.files) + sum(f.total_files for f in self.folders)

    @property
    def total_size(self) -> int:
        return sum(f.size or 0 for f in self.files) + sum(f.total_size for f in self.folders)


def build_listing(
    drive: DriveClient,
    root_id: str,
    root_name: str,
    known_keys: dict[str, str],
    key_order: dict[str, int],
    max_depth: int = MAX_DEPTH,
) -> FolderEntry:
    """Blocking: read the whole project folder. Bot-created folders keep the archive order, the rest is alphabetical."""

    def rank(entry: FolderEntry):
        return (0, key_order[entry.key], "") if entry.key in key_order else (1, 0, entry.name.lower())

    def walk(folder_id: str, name: str, depth: int) -> FolderEntry:
        entry = FolderEntry(folder_id, name, folder_link(folder_id), known_keys.get(folder_id))
        if depth >= max_depth:
            entry.truncated = True
            return entry
        try:
            children = drive.list_children(folder_id)
        except DriveError as exc:
            entry.error = str(exc)
            return entry
        for child in children:
            if child.is_folder:
                entry.folders.append(walk(child.id, child.name, depth + 1))
            elif not is_junk(child):
                entry.files.append(FileEntry(child.name, child.size, file_link(child.id)))
        entry.files.sort(key=lambda f: f.name.lower())
        entry.folders.sort(key=rank)
        return entry

    return walk(root_id, root_name, 0)


def render_listing(title: str, root: FolderEntry, *, limit: int = CHUNK_LIMIT) -> list[str]:
    """Render the tree as one or more Telegram HTML messages (numbered when split)."""
    lines: list[str] = []

    def emit(entry: FolderEntry, depth: int) -> None:
        pad = "  " * max(depth - 1, 0)
        if depth > 0:
            label = f'<a href="{entry.link}">{esc(entry.name)}</a>'
            if depth == 1:
                label = f"<b>{label}</b>"
            if entry.error:
                lines.append(f"{pad}{label} — ⚠️ could not read")
                return
            count = f" ({entry.total_files})" if entry.total_files else " — empty"
            lines.append(f"{pad}{label}{count}")
        file_pad = "  " * depth
        for f in entry.files:
            size = human_size(f.size) if f.size is not None else "—"
            lines.append(f"{file_pad}{esc(f.name)} · {size}")
        for sub in entry.folders:
            emit(sub, depth + 1)
        if entry.truncated:
            lines.append(f"{file_pad}… (deeper folders not shown)")

    emit(root, 0)
    total = root.total_files
    summary = f"{total} file{'s' if total != 1 else ''} · {human_size(root.total_size)}" if total else "no files yet"
    header = f'📂 <b>{esc(title)}</b> — {summary}\n<a href="{root.link}">Open project folder</a>'
    chunks: list[str] = []
    current = header + "\n"
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    if len(chunks) > 1:
        chunks = [f"{chunk}\n<i>({i}/{len(chunks)})</i>" for i, chunk in enumerate(chunks, 1)]
    return chunks
