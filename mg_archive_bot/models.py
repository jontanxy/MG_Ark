from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .constants import (
    AssetCategory,
    GroupStatus,
    PreviewStatus,
    ProjectStatus,
    Role,
    UserStatus,
)
from .db import Base
from .util import utcnow


def _enum(enum_cls, name: str):
    return Enum(enum_cls, name=name, native_enum=False, length=40, values_callable=lambda e: [m.value for m in e])


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[Role] = mapped_column(_enum(Role, "role"), default=Role.DESIGNER)
    status: Mapped[UserStatus] = mapped_column(_enum(UserStatus, "user_status"), default=UserStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def display_name(self) -> str:
        return self.name or (f"@{self.username}" if self.username else str(self.telegram_id))

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class MGGroup(Base):
    __tablename__ = "mg_groups"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[GroupStatus] = mapped_column(_enum(GroupStatus, "group_status"), default=GroupStatus.ACTIVE)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authorised_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Super Admin id, or None when the bot was removed

    @property
    def is_active(self) -> bool:
        return self.status == GroupStatus.ACTIVE

    @property
    def revoked_by_admin(self) -> bool:
        return self.status == GroupStatus.REVOKED and self.revoked_by is not None


class ProvisioningToken(Base):
    __tablename__ = "provisioning_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    created_by: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def is_valid(self, now: datetime) -> bool:
        return self.used_at is None and self.expires_at > now


class UnauthorisedChat(Base):
    """Chats the bot has been added to without authorisation (so it can leave them later)."""

    __tablename__ = "unauthorised_chats"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    warned: Mapped[bool] = mapped_column(Boolean, default=False)


class Collection(Base):
    """A folder directly under the archive root that groups several sub-projects (e.g. 'BF')."""

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    drive_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # created lazily on first project
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)


class ProjectTag(Base):
    __tablename__ = "project_tags"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[ProjectStatus] = mapped_column(
        _enum(ProjectStatus, "project_status"), default=ProjectStatus.DRAFT, index=True
    )

    has_timeline: Mapped[bool] = mapped_column(Boolean, default=False)
    has_contin_videos: Mapped[bool] = mapped_column(Boolean, default=False)
    has_contin_lyrics: Mapped[bool] = mapped_column(Boolean, default=False)
    has_psd: Mapped[bool] = mapped_column(Boolean, default=False)

    collection: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    event: Mapped[str] = mapped_column(String(200), default="")
    ministry: Mapped[str] = mapped_column(String(200), default="")
    style: Mapped[str] = mapped_column(String(200), default="")
    colours: Mapped[str] = mapped_column(String(200), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creator: Mapped[str] = mapped_column(String(200), default="")
    asset_types: Mapped[str] = mapped_column(String(200), default="")

    created_by: Mapped[int] = mapped_column(Integer, index=True)
    mg_group_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    collection_id: Mapped[int | None] = mapped_column(ForeignKey("collections.id", ondelete="SET NULL"), nullable=True, index=True)
    drive_root_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    drive_link: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tags: Mapped[list[Tag]] = relationship(Tag, secondary="project_tags", lazy="selectin", order_by=Tag.name)
    collection_folder: Mapped[Collection | None] = relationship(Collection, lazy="joined")
    folders: Mapped[list[ProjectFolder]] = relationship(
        "ProjectFolder", back_populates="project", lazy="selectin", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[Assignment]] = relationship(
        "Assignment", back_populates="project", lazy="selectin", cascade="all, delete-orphan"
    )
    previews: Mapped[list[PreviewAsset]] = relationship(
        "PreviewAsset", back_populates="project", lazy="selectin", cascade="all, delete-orphan"
    )

    def flag(self, attr: str | None) -> bool:
        if attr is None:
            return False
        if attr == "always":
            return True
        return bool(getattr(self, attr))

    def folder(self, key: str) -> ProjectFolder | None:
        for f in self.folders:
            if f.key == key:
                return f
        return None

    @property
    def tag_names(self) -> list[str]:
        return sorted(t.name for t in self.tags)

    @property
    def full_name(self) -> str:
        """'BF / Opening' for a sub-project, else just the project name."""
        return f"{self.collection_folder.name} / {self.name}" if self.collection_folder is not None else self.name

    @property
    def ready_previews(self) -> list[PreviewAsset]:
        return [p for p in self.previews if p.status == PreviewStatus.READY and p.preview_link]


class ProjectFolder(Base):
    __tablename__ = "project_folders"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_project_folder_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    drive_id: Mapped[str] = mapped_column(String(128))
    link: Mapped[str] = mapped_column(String(512))

    project: Mapped[Project] = relationship(Project, back_populates="folders")


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("project_id", "user_id", "category", name="uq_assignment"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    category: Mapped[AssetCategory] = mapped_column(_enum(AssetCategory, "asset_category"), default=AssetCategory.ALL)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(Project, back_populates="assignments")
    user: Mapped[User] = relationship(User, lazy="joined")


class PreviewAsset(Base):
    __tablename__ = "preview_assets"
    __table_args__ = (UniqueConstraint("project_id", "source_drive_id", name="uq_preview_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    category: Mapped[AssetCategory] = mapped_column(_enum(AssetCategory, "asset_category"))
    source_key: Mapped[str] = mapped_column(String(40), default="")
    source_drive_id: Mapped[str] = mapped_column(String(128))
    source_name: Mapped[str] = mapped_column(String(255))
    source_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    preview_drive_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    preview_link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    preview_name: Mapped[str] = mapped_column(String(255), default="")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[PreviewStatus] = mapped_column(_enum(PreviewStatus, "preview_status"), default=PreviewStatus.PENDING)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(Project, back_populates="previews")


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
