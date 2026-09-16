from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import ROLE_RANK, Role, UserStatus
from ..models import LoginAttempt, Setting, User
from ..security import hash_password, verify_password
from ..util import utcnow

PASSWORD_KEY = "access_password_hash"


class UserError(Exception):
    pass


def get_user(session: Session, telegram_id: int) -> User | None:
    return session.get(User, telegram_id)


def touch_profile(user: User, name: str, username: str | None) -> None:
    if name and user.name != name:
        user.name = name
    if username != user.username:
        user.username = username


def ensure_super_admin(session: Session, telegram_id: int, name: str, username: str | None) -> User:
    user = session.get(User, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, name=name, username=username, role=Role.SUPER_ADMIN, status=UserStatus.ACTIVE)
        session.add(user)
    else:
        user.role = Role.SUPER_ADMIN
        user.status = UserStatus.ACTIVE
        touch_profile(user, name, username)
    session.flush()
    return user


def reconcile_super_admin(session: Session, super_admin_id: int) -> list[int]:
    """Only the configured Telegram ID may hold SUPER_ADMIN; demote any other row (returns their ids)."""
    demoted: list[int] = []
    for user in session.scalars(select(User).where(User.role == Role.SUPER_ADMIN)):
        if user.telegram_id != super_admin_id:
            user.role = Role.TEAM_LEAD
            demoted.append(user.telegram_id)
    session.flush()
    return demoted


def register_designer(session: Session, telegram_id: int, name: str, username: str | None) -> User:
    existing = session.get(User, telegram_id)
    if existing is not None:
        if existing.status == UserStatus.REVOKED:
            raise UserError("This account has been revoked and cannot re-register.")
        touch_profile(existing, name, username)
        return existing
    user = User(telegram_id=telegram_id, name=name, username=username, role=Role.DESIGNER, status=UserStatus.ACTIVE)
    session.add(user)
    session.flush()
    return user


def list_users(session: Session) -> list[User]:
    users = session.scalars(select(User)).all()
    return sorted(users, key=lambda u: (-ROLE_RANK[u.role], u.status != UserStatus.ACTIVE, u.display_name.lower()))


def list_active_users(session: Session) -> list[User]:
    return [u for u in list_users(session) if u.status == UserStatus.ACTIVE]


def set_role(session: Session, telegram_id: int, role: Role, super_admin_id: int) -> User:
    user = session.get(User, telegram_id)
    if user is None:
        raise UserError("User not found.")
    if telegram_id == super_admin_id or user.role == Role.SUPER_ADMIN:
        raise UserError("The Super Admin role is fixed and cannot be changed.")
    if role == Role.SUPER_ADMIN:
        raise UserError("Only the configured Super Admin can hold that role.")
    user.role = role
    session.flush()
    return user


def revoke_user(session: Session, telegram_id: int, super_admin_id: int) -> User:
    user = session.get(User, telegram_id)
    if user is None:
        raise UserError("User not found.")
    if telegram_id == super_admin_id or user.role == Role.SUPER_ADMIN:
        raise UserError("The Super Admin cannot be revoked.")
    user.status = UserStatus.REVOKED
    session.flush()
    return user


def restore_user(session: Session, telegram_id: int) -> User:
    user = session.get(User, telegram_id)
    if user is None:
        raise UserError("User not found.")
    user.status = UserStatus.ACTIVE
    session.flush()
    return user


# ----------------------------------------------------------------------------------------
# Access password
# ----------------------------------------------------------------------------------------


def get_password_hash(session: Session) -> str | None:
    row = session.get(Setting, PASSWORD_KEY)
    return row.value if row and row.value else None


def seed_password_if_missing(session: Session, initial_password: str) -> bool:
    if get_password_hash(session):
        return False
    if not initial_password:
        raise UserError("No access password is set. Provide INITIAL_ACCESS_PASSWORD in .env for the first run.")
    session.merge(Setting(key=PASSWORD_KEY, value=hash_password(initial_password)))
    session.flush()
    return True


def set_access_password(session: Session, new_password: str) -> None:
    if len(new_password) < 8:
        raise UserError("Password must be at least 8 characters.")
    session.merge(Setting(key=PASSWORD_KEY, value=hash_password(new_password)))
    session.flush()


def verify_access_password(session: Session, password: str) -> bool:
    return verify_password(password, get_password_hash(session))


# ----------------------------------------------------------------------------------------
# Brute-force lockout
# ----------------------------------------------------------------------------------------


def lock_remaining_seconds(session: Session, telegram_id: int) -> int:
    row = session.get(LoginAttempt, telegram_id)
    if row is None or row.locked_until is None:
        return 0
    remaining = (row.locked_until - utcnow()).total_seconds()
    return int(remaining) + 1 if remaining > 0 else 0


def record_failed_login(session: Session, telegram_id: int, max_failures: int, lockout_minutes: int) -> tuple[int, int]:
    """Record a failure; return (failures so far, lock seconds if now locked else 0)."""
    now = utcnow()
    row = session.get(LoginAttempt, telegram_id)
    if row is None:
        row = LoginAttempt(telegram_id=telegram_id, failed_count=0)
        session.add(row)
    if row.locked_until and row.locked_until <= now:
        row.failed_count = 0
        row.locked_until = None
    row.failed_count += 1
    row.last_attempt_at = now
    lock_seconds = 0
    if row.failed_count >= max_failures:
        row.locked_until = now + timedelta(minutes=lockout_minutes)
        row.failed_count = 0
        lock_seconds = lockout_minutes * 60
    session.flush()
    return row.failed_count, lock_seconds


def clear_login_attempts(session: Session, telegram_id: int) -> None:
    row = session.get(LoginAttempt, telegram_id)
    if row is not None:
        session.delete(row)
        session.flush()
