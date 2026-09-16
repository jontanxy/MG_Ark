from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import GroupStatus
from ..models import MGGroup, Project, ProvisioningToken, UnauthorisedChat
from ..security import generate_provisioning_token, normalise_token
from ..util import utcnow


class GroupError(Exception):
    pass


def create_token(session: Session, created_by: int, ttl_hours: int) -> ProvisioningToken:
    """Create a fresh single-use token; earlier unused tokens of the same creator are expired."""
    now = utcnow()
    for old in session.scalars(
        select(ProvisioningToken).where(ProvisioningToken.created_by == created_by, ProvisioningToken.used_at.is_(None))
    ):
        old.expires_at = now
    for _ in range(10):
        token = generate_provisioning_token()
        if session.scalar(select(ProvisioningToken).where(ProvisioningToken.token == token)) is None:
            break
    else:  # pragma: no cover - astronomically unlikely
        raise GroupError("Could not generate a unique token, please retry.")
    row = ProvisioningToken(token=token, created_by=created_by, created_at=now, expires_at=now + timedelta(hours=ttl_hours))
    session.add(row)
    session.flush()
    return row


def find_valid_token(session: Session, raw: str) -> ProvisioningToken | None:
    token = normalise_token(raw)
    if not token:
        return None
    row = session.scalar(select(ProvisioningToken).where(ProvisioningToken.token == token))
    if row is None or not row.is_valid(utcnow()):
        return None
    return row


def pending_tokens_for(session: Session, user_id: int) -> list[ProvisioningToken]:
    now = utcnow()
    rows = session.scalars(
        select(ProvisioningToken).where(ProvisioningToken.created_by == user_id, ProvisioningToken.used_at.is_(None))
    ).all()
    return [r for r in rows if r.is_valid(now)]


def get_group(session: Session, chat_id: int) -> MGGroup | None:
    return session.get(MGGroup, chat_id)


def is_group_authorised(session: Session, chat_id: int) -> bool:
    group = session.get(MGGroup, chat_id)
    return bool(group and group.status == GroupStatus.ACTIVE)


def authorise_group(session: Session, chat_id: int, title: str, token: ProvisioningToken) -> MGGroup:
    now = utcnow()
    if not token.is_valid(now):
        raise GroupError("Token is invalid, expired or already used.")
    group = session.get(MGGroup, chat_id)
    if group is None:
        group = MGGroup(chat_id=chat_id, title=title, status=GroupStatus.ACTIVE, created_by=token.created_by, authorised_at=now)
        session.add(group)
    else:
        if group.revoked_by_admin:
            raise GroupError("This group was revoked by the Super Admin. Ask them to restore it from /groups before re-activating.")
        group.title = title or group.title
        group.status = GroupStatus.ACTIVE
        group.created_by = token.created_by
        group.authorised_at = now
        group.revoked_at = None
        group.revoked_by = None
    token.used_at = now
    token.used_chat_id = chat_id
    forget_unauthorised_chat(session, chat_id)
    session.flush()
    return group


def list_groups(session: Session) -> list[MGGroup]:
    return list(session.scalars(select(MGGroup).order_by(MGGroup.authorised_at)).all())


def list_active_groups(session: Session) -> list[MGGroup]:
    return [g for g in list_groups(session) if g.status == GroupStatus.ACTIVE]


def revoke_group(session: Session, chat_id: int, revoked_by: int | None = None) -> MGGroup:
    """Revoke a group. ``revoked_by`` is the Super Admin id (a deliberate decision) or None (bot was removed)."""
    group = session.get(MGGroup, chat_id)
    if group is None:
        raise GroupError("Group not found.")
    group.status = GroupStatus.REVOKED
    group.revoked_at = utcnow()
    group.revoked_by = revoked_by
    session.flush()
    return group


def restore_group(session: Session, chat_id: int) -> MGGroup:
    group = session.get(MGGroup, chat_id)
    if group is None:
        raise GroupError("Group not found.")
    group.status = GroupStatus.ACTIVE
    group.revoked_at = None
    group.revoked_by = None
    session.flush()
    return group


def migrate_group(session: Session, old_chat_id: int, new_chat_id: int) -> bool:
    """A basic group became a supergroup: carry authorisation and project links over to the new chat id."""
    if old_chat_id == new_chat_id:
        return False
    group = session.get(MGGroup, old_chat_id)
    moved = False
    if group is not None:
        existing = session.get(MGGroup, new_chat_id)
        if existing is None:
            group.chat_id = new_chat_id
        else:
            existing.status = group.status if existing.status != GroupStatus.ACTIVE else existing.status
            session.delete(group)
        moved = True
    for project in session.scalars(select(Project).where(Project.mg_group_chat_id == old_chat_id)):
        project.mg_group_chat_id = new_chat_id
        moved = True
    stale = session.get(UnauthorisedChat, old_chat_id)
    if stale is not None:
        session.delete(stale)
    session.flush()
    return moved


def update_group_title(session: Session, chat_id: int, title: str) -> None:
    group = session.get(MGGroup, chat_id)
    if group is not None and title and group.title != title:
        group.title = title
        session.flush()


def note_unauthorised_chat(session: Session, chat_id: int, title: str) -> UnauthorisedChat:
    row = session.get(UnauthorisedChat, chat_id)
    if row is None:
        row = UnauthorisedChat(chat_id=chat_id, title=title or "")
        session.add(row)
        session.flush()
    return row


def forget_unauthorised_chat(session: Session, chat_id: int) -> None:
    row = session.get(UnauthorisedChat, chat_id)
    if row is not None:
        session.delete(row)
        session.flush()


def stale_unauthorised_chats(session: Session, older_than_minutes: int) -> list[UnauthorisedChat]:
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    rows = session.scalars(select(UnauthorisedChat).where(UnauthorisedChat.first_seen_at <= cutoff)).all()
    return list(rows)
