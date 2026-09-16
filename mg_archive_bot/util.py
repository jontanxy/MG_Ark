from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone support; everything stored is UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_tz(name: str | None) -> tzinfo:
    """Return an IANA timezone: *name* if given, else the host zone (TZ env / /etc/localtime), else a fixed offset."""
    if name:
        return ZoneInfo(name)
    for candidate in (os.environ.get("TZ"), _localtime_key()):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (KeyError, ValueError, OSError):
                continue
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def _localtime_key() -> str | None:
    """IANA key from the /etc/localtime symlink (macOS and most Linux distributions)."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    idx = target.find(marker)
    return target[idx + len(marker) :] if idx >= 0 else None


def to_local(dt: datetime | None, tz: tzinfo) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def fmt_dt(dt: datetime | None, tz: tzinfo) -> str:
    local = to_local(dt, tz)
    return local.strftime("%d %b %Y %H:%M") if local else "never"


def esc(value: object) -> str:
    """HTML-escape a value for Telegram HTML parse mode."""
    return html.escape("" if value is None else str(value), quote=False)


def normalise_terms(raw: str) -> list[str]:
    """Split a comma-separated (fallback: whitespace) query into trimmed, lower-cased, unique terms."""
    if not raw:
        return []
    parts = raw.split(",") if "," in raw else raw.split()
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        term = re.sub(r"\s+", " ", part).strip().lower().lstrip("#")
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def human_size(num: int | None) -> str:
    if num is None:
        return "?"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'
