"""HTML message builders for private replies and MG group posts."""
from __future__ import annotations

from datetime import tzinfo

from ..constants import (
    CATEGORY_LABELS,
    STATUS_LABELS,
    AssetCategory,
    FolderSpec,
    ProjectStatus,
    folder_path_label,
)
from ..models import Project, User
from ..util import esc, fmt_dt, mention
from .projects import assignees_for, required_leaves
from .validation import ValidationReport


def _mentions(users: list[User]) -> str:
    return ", ".join(mention(u.telegram_id, u.display_name) for u in users) if users else "—"


def status_line(project: Project) -> str:
    return STATUS_LABELS.get(project.status, project.status.value)


def tags_line(project: Project) -> str:
    return " ".join(f"#{esc(t)}" for t in project.tag_names) if project.tags else "<i>no tags</i>"


def announcement(project: Project, tree: list[FolderSpec]) -> str:
    lines = [f"📦 <b>New archive: {esc(project.full_name)}</b>", ""]
    if project.drive_link:
        lines.append(f'📁 <a href="{project.drive_link}">Open project folder</a>')
        lines.append("")
    lines.append("<b>Required uploads</b>")
    for spec in required_leaves(project, tree):
        folder = project.folder(spec.key)
        label = esc(folder_path_label(tree, spec.key))
        link = f' — <a href="{folder.link}">open</a>' if folder else ""
        who = assignees_for(project, spec.category)
        lines.append(f"• {label}{link}" + (f"  👤 {_mentions(who)}" if who else ""))
    everyone = assignees_for(project, None)
    lines.append("")
    lines.append(f"👥 <b>Assigned:</b> {_mentions(everyone)}")
    lines.append("")
    lines.append("Upload your files into the folders above. I check Google Drive automatically and will post progress here.")
    return "\n".join(lines)


def progress_block(project: Project, report: ValidationReport, tree: list[FolderSpec], *, with_links: bool = False) -> list[str]:
    lines: list[str] = []
    for item in report.required_items:
        folder = project.folder(item.key)
        label = esc(item.label)
        if with_links and folder:
            label = f'<a href="{folder.link}">{label}</a>'
        if item.error:
            lines.append(f"⚠️ {label} — could not check Google Drive")
        elif item.ok:
            lines.append(f"✅ {label} ({item.file_count} file{'s' if item.file_count != 1 else ''})")
        else:
            who = assignees_for(project, AssetCategory(item.category)) if item.category != "ALL" else []
            note = f" — {esc(item.note)}" if item.note and item.note != "folder not provisioned" else ""
            lines.append(f"❌ {label}{note}" + (f"  👤 {_mentions(who)}" if who else ""))
    return lines


def progress_message(project: Project, report: ValidationReport, tree: list[FolderSpec], tz: tzinfo) -> str:
    lines = [f"📊 <b>{esc(project.full_name)}</b> — {status_line(project)}", ""]
    lines += progress_block(project, report, tree, with_links=True)
    missing = report.missing
    lines.append("")
    if report.had_errors:
        lines.append("⚠️ Google Drive could not be checked completely — status unchanged, try again later.")
    elif project.status == ProjectStatus.ARCHIVED:
        lines.append("Archive verified and closed.")
    elif missing:
        lines.append(f"{len(missing)} of {len(report.required_items)} required folders still empty.")
    else:
        lines.append("All declared assets are uploaded — awaiting Team Lead verification.")
    lines.append(f"<i>Checked {esc(fmt_dt(project.last_validated_at, tz))}</i>")
    return "\n".join(lines)


def reminder_message(project: Project, report: ValidationReport, tree: list[FolderSpec]) -> str:
    lines = [f"⏰ <b>Reminder: {esc(project.full_name)}</b>", "", "Still missing:"]
    for item in report.missing:
        folder = project.folder(item.key)
        label = f'<a href="{folder.link}">{esc(item.label)}</a>' if folder else esc(item.label)
        who = assignees_for(project, AssetCategory(item.category)) if item.category != "ALL" else []
        lines.append(f"❌ {label}" + (f"  👤 {_mentions(who)}" if who else ""))
    if project.drive_link:
        lines += ["", f'📁 <a href="{project.drive_link}">Project folder</a>']
    return "\n".join(lines)


def ready_message(project: Project) -> str:
    return (
        f"🔵 <b>{esc(project.full_name)}</b> — all declared assets are uploaded.\n"
        "Ready for Team Lead verification."
    )


def archived_message(project: Project, verifier: User | None) -> str:
    who = f" by {mention(verifier.telegram_id, verifier.display_name)}" if verifier else ""
    return f"✅ <b>{esc(project.full_name)}</b> has been verified and archived{who}."


def reopened_message(project: Project) -> str:
    return f"🟢 <b>{esc(project.full_name)}</b> has been reopened for further uploads."


def project_details(project: Project, report: ValidationReport | None, tree: list[FolderSpec], tz: tzinfo, creator: User | None = None) -> str:
    lines = [f"🎬 <b>{esc(project.full_name)}</b>", status_line(project), "", tags_line(project), ""]
    meta = [
        ("Collection", project.collection),
        ("Event", project.event),
        ("Ministry", project.ministry),
        ("Style", project.style),
        ("Colours", project.colours),
        ("Year", project.year),
        ("Creator", project.creator),
        ("Asset type", project.asset_types),
    ]
    for label, value in meta:
        if value:
            lines.append(f"<b>{label}:</b> {esc(value)}")
    if project.description:
        lines += ["", esc(project.description)]
    declared = [CATEGORY_LABELS[c] for c in (AssetCategory.TIMELINE, AssetCategory.CONTIN_VIDEOS, AssetCategory.CONTIN_LYRICS, AssetCategory.PSD) if project.flag({AssetCategory.TIMELINE: "has_timeline", AssetCategory.CONTIN_VIDEOS: "has_contin_videos", AssetCategory.CONTIN_LYRICS: "has_contin_lyrics", AssetCategory.PSD: "has_psd"}[c])]
    lines += ["", f"<b>Declared assets:</b> {esc(', '.join(declared) if declared else 'Working files only')}"]
    ready = project.ready_previews
    lines.append(f"<b>Previews:</b> {len(ready)} available")
    if project.drive_link:
        lines.append(f'<b>Archive:</b> <a href="{project.drive_link}">Google Drive</a>')
    who = assignees_for(project, None)
    if who:
        lines.append(f"<b>Assigned:</b> {_mentions(who)}")
    if creator:
        lines.append(f"<b>Created by:</b> {esc(creator.display_name)} · {esc(fmt_dt(project.created_at, tz))}")
    if report is not None:
        lines += ["", "<b>Latest check</b>"] + progress_block(project, report, tree) + [f"<i>{esc(fmt_dt(project.last_validated_at, tz))}</i>"]
    return "\n".join(lines)


def search_result_card(project: Project) -> str:
    n = len(project.ready_previews)
    previews = f"{n} preview{'s' if n != 1 else ''} available" if n else "no previews yet"
    return f"🎬 <b>{esc(project.full_name)}</b>\n\n{tags_line(project)}\n\n{previews} · {status_line(project)}"
