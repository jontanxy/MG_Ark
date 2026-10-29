from __future__ import annotations

import enum
from dataclasses import dataclass
from collections.abc import Mapping


class Role(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    TEAM_LEAD = "TEAM_LEAD"
    DESIGNER = "DESIGNER"
    LIGHTS = "LIGHTS"  # view-only: search, previews, archive links; never assigned to projects


ROLE_RANK: dict[Role, int] = {Role.LIGHTS: 0, Role.DESIGNER: 0, Role.TEAM_LEAD: 1, Role.SUPER_ADMIN: 2}
ROLE_LABELS: dict[Role, str] = {
    Role.SUPER_ADMIN: "Super Admin",
    Role.TEAM_LEAD: "Team Lead",
    Role.DESIGNER: "Designer",
    Role.LIGHTS: "Lights",
}
# Roles that upload assets and can therefore be assigned to a project's folders.
CONTRIBUTOR_ROLES = frozenset({Role.DESIGNER, Role.TEAM_LEAD, Role.SUPER_ADMIN})


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class GroupStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ProjectStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INCOMPLETE = "INCOMPLETE"
    READY_FOR_VERIFICATION = "READY_FOR_VERIFICATION"
    ARCHIVED = "ARCHIVED"
    CANCELLED = "CANCELLED"  # revoked: Drive folder trashed, no tracking, hidden from search


SCANNABLE_STATUSES = frozenset(
    {ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION}
)

STATUS_LABELS: dict[ProjectStatus, str] = {
    ProjectStatus.DRAFT: "📝 Draft",
    ProjectStatus.ACTIVE: "🟢 Active",
    ProjectStatus.INCOMPLETE: "🟠 Incomplete",
    ProjectStatus.READY_FOR_VERIFICATION: "🔵 Ready for verification",
    ProjectStatus.ARCHIVED: "✅ Archived",
    ProjectStatus.CANCELLED: "🗑 Cancelled",
}

REVOCABLE_STATUSES = frozenset({ProjectStatus.ACTIVE, ProjectStatus.INCOMPLETE, ProjectStatus.READY_FOR_VERIFICATION})


class AssetCategory(str, enum.Enum):
    ALL = "ALL"
    WORKING_FILE = "WORKING_FILE"
    TIMELINE = "TIMELINE"
    CONTIN_VIDEOS = "CONTIN_VIDEOS"
    CONTIN_LYRICS = "CONTIN_LYRICS"
    TITLEBARS = "TITLEBARS"
    PSD = "PSD"


CATEGORY_LABELS: dict[AssetCategory, str] = {
    AssetCategory.ALL: "All assets",
    AssetCategory.WORKING_FILE: "Working File",
    AssetCategory.TIMELINE: "Timeline",
    AssetCategory.CONTIN_VIDEOS: "Contin Videos",
    AssetCategory.CONTIN_LYRICS: "Contin Lyrics",
    AssetCategory.TITLEBARS: "Titlebars",
    AssetCategory.PSD: "PSD",
}

# Project boolean attribute that switches each declarable category on. The order is the order of the
# declaration buttons, the assignment list and the derived "Asset types" text.
CATEGORY_FLAGS: dict[AssetCategory, str] = {
    AssetCategory.TIMELINE: "has_timeline",
    AssetCategory.CONTIN_VIDEOS: "has_contin_videos",
    AssetCategory.CONTIN_LYRICS: "has_contin_lyrics",
    AssetCategory.TITLEBARS: "has_titlebars",
    AssetCategory.PSD: "has_psd",
}


class PreviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


DEFAULT_FOLDER_NAMES: dict[str, str] = {
    "working_file": "Working File",
    "fonts": "Fonts",
    "ae": "AE",
    "psd": "PSD",
    "final_render": "Final Render",
    "timeline": "Timeline",
    "contin_videos": "Contin Videos",
    "contin_lyrics": "Contin Lyrics",
    "titlebars": "Titlebars",
    "prores": "ProRes 4444",
    "hap": "Hap/Hap Alpha",
    "png": "PNG",
    "previews": "_Previews",
}


@dataclass(frozen=True)
class FolderSpec:
    key: str
    parent_key: str | None
    name: str
    category: AssetCategory | None = None
    # Project attribute that makes this leaf *required*; "always" means unconditional; None = never validated.
    required_when: str | None = None
    # Project attribute that must be true for the folder to be created at all (PSD and Titlebars).
    create_when: str | None = None

    @property
    def is_leaf(self) -> bool:
        return self.required_when is not None


def build_folder_tree(names: Mapping[str, str] | None = None) -> list[FolderSpec]:
    """Return the fixed archive tree (parents before children). ``root`` is the project folder itself."""
    n = dict(DEFAULT_FOLDER_NAMES)
    if names:
        n.update({k: v for k, v in names.items() if v})
    return [
        FolderSpec("working_file", "root", n["working_file"]),
        FolderSpec("fonts", "working_file", n["fonts"], AssetCategory.WORKING_FILE, "always"),
        FolderSpec("ae", "working_file", n["ae"], AssetCategory.WORKING_FILE, "always"),
        FolderSpec("psd", "working_file", n["psd"], AssetCategory.PSD, "has_psd", "has_psd"),
        FolderSpec("final_render", "root", n["final_render"]),
        FolderSpec("timeline", "final_render", n["timeline"], AssetCategory.TIMELINE),
        FolderSpec("timeline_prores", "timeline", n["prores"], AssetCategory.TIMELINE, "has_timeline"),
        FolderSpec("timeline_hap", "timeline", n["hap"], AssetCategory.TIMELINE, "has_timeline"),
        FolderSpec("contin_videos", "final_render", n["contin_videos"], AssetCategory.CONTIN_VIDEOS),
        FolderSpec("contin_prores", "contin_videos", n["prores"], AssetCategory.CONTIN_VIDEOS, "has_contin_videos"),
        FolderSpec("contin_hap", "contin_videos", n["hap"], AssetCategory.CONTIN_VIDEOS, "has_contin_videos"),
        FolderSpec("contin_lyrics", "final_render", n["contin_lyrics"], AssetCategory.CONTIN_LYRICS),
        FolderSpec("lyrics_png", "contin_lyrics", n["png"], AssetCategory.CONTIN_LYRICS, "has_contin_lyrics"),
        # Titlebars were added after the first archives existed, so (like PSD) the folder only appears in
        # projects that declare it; older projects are left untouched. It is one folder, without format folders.
        FolderSpec("titlebars", "final_render", n["titlebars"], AssetCategory.TITLEBARS, "has_titlebars", "has_titlebars"),
        FolderSpec("previews", "root", n["previews"]),
    ]


# Folders that an earlier layout had and this one does not: for one evening Titlebars was split into the two
# format folders. The bot forgets them (see projects.forget_obsolete_folders); on Google Drive they are left alone.
OBSOLETE_FOLDER_KEYS = frozenset({"titlebars_prores", "titlebars_hap"})


def folder_path_label(tree: list[FolderSpec], key: str) -> str:
    """Human path such as ``Final Render / Timeline / ProRes 4444``."""
    by_key = {f.key: f for f in tree}
    parts: list[str] = []
    cur = by_key.get(key)
    while cur is not None:
        parts.append(cur.name)
        cur = by_key.get(cur.parent_key) if cur.parent_key else None
    return " / ".join(reversed(parts))


# Leaves whose ProRes files become MP4 previews, with the flag that enables them.
PREVIEW_SOURCE_KEYS: dict[str, str] = {
    "timeline_prores": "has_timeline",
    "contin_prores": "has_contin_videos",
}

PREVIEW_SOURCE_CATEGORY: dict[str, AssetCategory] = {
    "timeline_prores": AssetCategory.TIMELINE,
    "contin_prores": AssetCategory.CONTIN_VIDEOS,
}

VIDEO_EXTENSIONS = frozenset({".mov", ".mxf", ".mp4", ".m4v", ".avi", ".mkv", ".prores"})

FOLDER_MIME = "application/vnd.google-apps.folder"

# Metadata fields editable by Team Leads (attribute -> label).
METADATA_FIELDS: dict[str, str] = {
    "collection": "Collection",
    "event": "Event",
    "ministry": "Ministry",
    "style": "Style",
    "colours": "Colours",
    "year": "Year",
    "creator": "Creator",
    "asset_types": "Asset type",
    "tags": "Tags",
    "description": "Description",
}
