"""MP4 preview generation from ProRes masters.

Previews live only on Google Drive (``<Project>/_Previews/``); the bot hands out their Drive links and never
uploads video to Telegram. Blocking parts (download → ffmpeg → upload) live in :func:`process_preview` and must
run in a thread. Database bookkeeping happens on the event loop via :func:`plan_previews` / :func:`record_result`.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings
from ..constants import PREVIEW_SOURCE_CATEGORY, PREVIEW_SOURCE_KEYS, VIDEO_EXTENSIONS, PreviewStatus
from ..models import PreviewAsset, Project
from .drive import DriveClient, DriveError, DriveFile, file_link
from .validation import is_video

log = logging.getLogger(__name__)

_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


class PreviewError(Exception):
    pass


@dataclass(frozen=True)
class PreviewOrphan:
    """A preview whose source vanished: its Drive file should be removed."""

    preview_drive_id: str | None


def safe_filename(name: str, fallback: str = "preview") -> str:
    """Reduce an arbitrary Drive name to one harmless path component (Drive names may contain '/' and '..')."""
    stem = Path(name.replace("\\", "/")).name if name else ""
    cleaned = re.sub(r"[^A-Za-z0-9 ._()\[\]-]+", "_", stem).strip(" .")
    return cleaned or fallback


def preview_file_name(source_name: str) -> str:
    base = safe_filename(source_name)
    stem = Path(base).stem or "preview"
    return f"{stem}.mp4"


@dataclass(frozen=True)
class PreviewJob:
    project_id: int
    preview_id: int
    source_key: str
    source: DriveFile
    previews_folder_id: str
    requested_by: int | None = None

    @property
    def dedupe_key(self) -> tuple[int, str, str]:
        return (self.project_id, self.source.id, self.source.fingerprint)


@dataclass
class PreviewResult:
    status: PreviewStatus
    preview_name: str = ""
    preview_drive_id: str | None = None
    size_bytes: int | None = None
    error: str = ""


def plan_previews(
    session: Session,
    project: Project,
    source_files: dict[str, list[DriveFile]],
    *,
    force: bool = False,
    orphans: list[PreviewOrphan] | None = None,
) -> list[PreviewJob]:
    """Create/refresh PENDING rows for new or changed ProRes sources; drop rows whose source vanished.

    Failed conversions are not retried automatically (``force=True`` — the Team Lead's *Generate previews* — retries them).
    """
    previews_folder = project.folder("previews")
    if previews_folder is None:
        return []
    jobs: list[PreviewJob] = []
    seen_ids: set[str] = set()
    existing = {p.source_drive_id: p for p in project.previews}
    for key, flag in PREVIEW_SOURCE_KEYS.items():
        if not project.flag(flag):
            continue
        for f in source_files.get(key, []):
            if not is_video(f):
                continue
            seen_ids.add(f.id)
            row = existing.get(f.id)
            if row is None:
                row = PreviewAsset(
                    project_id=project.id,
                    category=PREVIEW_SOURCE_CATEGORY[key],
                    source_key=key,
                    source_drive_id=f.id,
                    source_name=f.name,
                    source_fingerprint=f.fingerprint,
                    status=PreviewStatus.PENDING,
                )
                session.add(row)
                project.previews.append(row)
                session.flush()
            elif row.source_fingerprint == f.fingerprint and row.status == PreviewStatus.READY:
                continue
            elif row.source_fingerprint == f.fingerprint and row.status == PreviewStatus.FAILED and not force:
                continue  # do not re-download multi-GB masters every scan; a manual "Generate previews" retries
            elif row.source_fingerprint == f.fingerprint and row.status == PreviewStatus.PENDING:
                pass  # already queued (or a previous run was interrupted) — re-enqueue is idempotent
            else:
                row.source_fingerprint = f.fingerprint
                row.source_name = f.name
                row.status = PreviewStatus.PENDING
                row.error = ""
                session.flush()
            jobs.append(PreviewJob(project.id, row.id, key, f, previews_folder.drive_id))
    # Sources that disappeared (only for keys we actually scanned this time).
    scanned_keys = {k for k in PREVIEW_SOURCE_KEYS if k in source_files}
    for row in list(project.previews):
        if row.source_key in scanned_keys and row.source_drive_id not in seen_ids:
            if orphans is not None and row.preview_drive_id:
                orphans.append(PreviewOrphan(row.preview_drive_id))
            project.previews.remove(row)
            session.delete(row)
    session.flush()
    return jobs


def build_ffmpeg_command(ffmpeg: str, src: Path, dst: Path, max_width: int, crf: int) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vf",
        f"scale=w='trunc(min({max_width},iw)/2)*2':h=-2,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(dst),
    ]


def transcode(ffmpeg: str, src: Path, dst: Path, max_width: int, crf: int, timeout: int = 4 * 3600) -> Path:
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).exists():
        raise PreviewError(f"ffmpeg not found at '{ffmpeg}'. Install it (macOS: brew install ffmpeg) or set FFMPEG_PATH.")
    cmd = build_ffmpeg_command(ffmpeg, src, dst, max_width, crf)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise PreviewError(f"could not start ffmpeg: {exc}") from exc
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)
    try:
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise PreviewError("ffmpeg timed out") from exc
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS.discard(proc)
    if proc.returncode != 0 or not dst.exists():
        if proc.returncode and proc.returncode < 0:
            raise PreviewError("ffmpeg was stopped (bot shutting down)")
        tail = (stderr or "").strip().splitlines()[-5:]
        raise PreviewError("ffmpeg failed: " + " | ".join(tail) if tail else f"ffmpeg exit {proc.returncode}")
    return dst


def kill_active_transcodes() -> int:
    """Terminate running ffmpeg processes (called on shutdown so the process can exit promptly)."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        try:
            proc.kill()
        except OSError:  # pragma: no cover - already gone
            pass
    return len(procs)


def process_preview(job: PreviewJob, drive: DriveClient, settings: Settings, existing_preview_drive_id: str | None) -> PreviewResult:
    """Blocking: download the source, transcode it to a small MP4 and upload that to ``_Previews/`` on Drive."""
    work = settings.work_dir / f"p{job.project_id}-{safe_filename(job.source.id[-8:], 'src')}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    # Never use the Drive name as a path component: names may contain '/', '..' or be absolute.
    ext = job.source.extension if job.source.extension in VIDEO_EXTENSIONS else ".mov"
    src = work / f"source{ext}"
    out = work / "preview.mp4"
    preview_name = preview_file_name(job.source.name)
    try:
        needed = int((job.source.size or 0) * 1.3) + 200 * 1024 * 1024
        free = shutil.disk_usage(work).free
        if free < needed:
            raise PreviewError(f"insufficient disk space in {settings.work_dir}: need ~{needed // 2**20} MB, {free // 2**20} MB free")
        log.info("Preview: downloading %s (%s bytes)", job.source.name, job.source.size)
        drive.download(job.source.id, src)
        transcode(settings.ffmpeg_path, src, out, settings.preview_max_width, settings.preview_crf)
        size = out.stat().st_size
        src.unlink(missing_ok=True)  # free the multi-GB master before uploading

        if existing_preview_drive_id:
            try:
                drive.delete(existing_preview_drive_id)
            except DriveError as exc:  # pragma: no cover - best effort
                log.warning("Could not delete old preview %s: %s", existing_preview_drive_id, exc)
        uploaded = drive.upload(out, job.previews_folder_id, preview_name, "video/mp4")
        return PreviewResult(PreviewStatus.READY, preview_name, uploaded.id, size)
    except (DriveError, PreviewError, OSError) as exc:
        log.exception("Preview generation failed for %s", job.source.name)
        return PreviewResult(PreviewStatus.FAILED, preview_name, None, None, str(exc)[:1000])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def record_result(session: Session, preview_id: int, result: PreviewResult) -> PreviewAsset | None:
    row = session.get(PreviewAsset, preview_id)
    if row is None:  # source vanished meanwhile
        return None
    row.status = result.status
    row.error = result.error
    if result.status == PreviewStatus.READY:
        row.preview_name = result.preview_name
        row.preview_drive_id = result.preview_drive_id
        row.preview_link = file_link(result.preview_drive_id) if result.preview_drive_id else None
        row.size_bytes = result.size_bytes
    session.flush()
    return row


def remove_orphans(orphans: list[PreviewOrphan], drive: DriveClient) -> None:
    """Blocking, best effort: delete Drive previews whose source is gone."""
    for orphan in orphans:
        if orphan.preview_drive_id:
            try:
                drive.delete(orphan.preview_drive_id)
            except DriveError as exc:
                log.info("Could not delete orphaned preview %s: %s", orphan.preview_drive_id, exc)

