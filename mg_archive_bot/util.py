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


MAX_TERMS = 50
MAX_TERM_LENGTH = 100
TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_MESSAGE_LIMIT = 4000


def normalise_terms(raw: str, *, max_terms: int = MAX_TERMS, max_length: int = MAX_TERM_LENGTH) -> list[str]:
    """Split a comma-separated (fallback: whitespace) query into trimmed, lower-cased, unique terms.

    Bounded (``max_terms`` terms of at most ``max_length`` characters) so user input can never produce
    oversized messages or unbounded tag lists.
    """
    if not raw:
        return []
    parts = raw.split(",") if "," in raw else raw.split()
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        term = re.sub(r"\s+", " ", part).strip().lower().lstrip("#")[:max_length].strip()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
            if len(out) >= max_terms:
                break
    return out


def clip_message(text: str, limit: int = SAFE_MESSAGE_LIMIT) -> str:
    """Keep a Telegram HTML message under the size limit by dropping whole trailing lines.

    Our message builders never let an HTML tag span a line break, so cutting at a newline keeps the
    markup valid. Only reached with adversarial or pathological content.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 2)
    if cut <= 0:
        cut = limit - 2
    return text[:cut].rstrip() + "\n…"


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
