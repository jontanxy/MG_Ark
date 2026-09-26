"""Set the bot's access password from INITIAL_ACCESS_PASSWORD (or an interactive prompt) and clear login lockouts.

Usage:  python -m mg_archive_bot.tools.reset_password            # use INITIAL_ACCESS_PASSWORD from .env
        python -m mg_archive_bot.tools.reset_password --prompt   # type the password (not echoed)

INITIAL_ACCESS_PASSWORD is otherwise only read once, when the database is first created; editing .env later
does not change the password. The Super Admin can also change it from Telegram with /setpassword.
"""
from __future__ import annotations

import getpass
import sys

from pydantic import ValidationError
from sqlalchemy import delete

from ..config import Settings
from ..db import init_db, session_scope
from ..models import LoginAttempt
from ..services import users as user_service


def reset(settings: Settings, password: str) -> None:
    """Store *password* as the access password and clear every login lockout."""
    init_db(settings.database_url)
    with session_scope() as session:
        user_service.set_access_password(session, password)
        session.execute(delete(LoginAttempt))


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        settings = settings or Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print("Configuration error (check your .env):", file=sys.stderr)
        for err in exc.errors():
            print(f"  - {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}", file=sys.stderr)
        return 2
    if "--prompt" in args:
        password = getpass.getpass("New access password: ")
        if password != getpass.getpass("Repeat it: "):
            print("The two entries differ; nothing changed.", file=sys.stderr)
            return 1
        source = "the prompt"
    else:
        password = settings.initial_access_password
        source = "INITIAL_ACCESS_PASSWORD"
        if not password:
            print("INITIAL_ACCESS_PASSWORD is empty in .env; use --prompt instead.", file=sys.stderr)
            return 2
    try:
        reset(settings, password)
    except user_service.UserError as exc:
        print(f"Not changed: {exc}", file=sys.stderr)
        return 1
    print(f"Access password set from {source}; login lockouts cleared. Registered users are unaffected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
