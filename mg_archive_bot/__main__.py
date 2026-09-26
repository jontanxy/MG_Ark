from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from pydantic import ValidationError
from telegram.error import InvalidToken

from .bot.app import ALLOWED_UPDATES, build_application
from .config import Settings
from .db import init_db, session_scope
from .services import projects as project_service
from .services import users as user_service
from .services.drive import DriveError, build_drive_client
from .services.sheets import build_sheets_client


def configure_logging(settings: Settings) -> logging.Logger:
    """Console + rotating file logging. Third-party loggers are clamped so LOG_LEVEL=DEBUG never records
    message text (python-telegram-bot logs whole updates at DEBUG, i.e. passwords) or request URLs."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(settings.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for name in ("telegram", "googleapiclient", "google"):
        logging.getLogger(name).setLevel(logging.INFO)
    for name in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logging.getLogger("mg_archive_bot")


def _restrict(path: Path, mode: int = 0o600) -> None:
    """Make a file/directory the bot created private to its own user (best effort; no-op on Windows)."""
    try:
        if path.exists():
            os.chmod(path, mode)
    except OSError:  # pragma: no cover - permissions not supported
        pass


def check_drive_root(settings: Settings, drive, log: logging.Logger) -> None:
    """Fail fast when the archive root is unreachable."""
    if settings.google_auth_mode == "fake":
        return
    root = drive.get_file(settings.drive_root_folder_id)
    if root is None:
        raise DriveError(
            f"DRIVE_ROOT_FOLDER_ID={settings.drive_root_folder_id} is not visible to the bot's Google identity. "
            "Share the folder (or its Shared Drive) with the service account e-mail / OAuth user."
        )
    if not root.is_folder:
        raise DriveError("DRIVE_ROOT_FOLDER_ID does not point at a folder")
    if root.drive_id:
        log.info("Archive root '%s' lives in Shared Drive %s", root.name, root.drive_id)
    else:
        log.info("Archive root '%s' is in a My Drive", root.name)
        if settings.google_auth_mode == "service_account":
            log.warning(
                "Service account + My Drive: folders the bot creates will be owned by the service account and count "
                "against its quota; preview uploads may fail. A Shared Drive is strongly recommended."
            )
    children = drive.list_children(root.id)  # proves listing works with the resolved corpora/driveId
    log.info("Archive root contains %d item(s)", len(children))


def _warn_if_readable(path: Path, log: logging.Logger) -> None:
    try:
        if path.exists() and os.stat(path).st_mode & 0o077:
            log.warning("%s is readable by other accounts on this machine; run: chmod 600 %s", path, path)
    except OSError:  # pragma: no cover
        pass


def main() -> int:
    os.umask(0o077)  # every file this process creates (db, WAL, rotated logs, downloads) is private to its user
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print("Configuration error (check your .env):", file=sys.stderr)
        for err in exc.errors():
            print(f"  - {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}", file=sys.stderr)
        return 2
    log = configure_logging(settings)
    for path in (Path(".env"), settings.google_service_account_file, settings.google_oauth_token_file):
        _warn_if_readable(path, log)

    problems = settings.validate_runtime()
    if problems:
        for p in problems:
            log.error("Configuration problem: %s", p)
        return 2

    settings.work_dir.mkdir(parents=True, exist_ok=True)
    _restrict(settings.work_dir, 0o700)  # downloaded masters are private to the bot's user
    for leftover in settings.work_dir.iterdir():  # interrupted preview jobs are re-planned by the next scan
        shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink(missing_ok=True)
    init_db(settings.database_url)
    if settings.database_url.startswith("sqlite:///"):
        db_file = Path(settings.database_url.removeprefix("sqlite:///"))
        _restrict(db_file.parent, 0o700)
        for sibling in (db_file, db_file.with_name(db_file.name + "-wal"), db_file.with_name(db_file.name + "-shm")):
            _restrict(sibling)  # user ids + password hash
    if settings.log_file:
        _restrict(settings.log_file)
    with session_scope() as session:
        try:
            if user_service.seed_password_if_missing(session, settings.initial_access_password):
                log.info("Access password seeded from INITIAL_ACCESS_PASSWORD")
            elif settings.initial_access_password and not user_service.verify_access_password(
                session, settings.initial_access_password
            ):
                log.warning(
                    "INITIAL_ACCESS_PASSWORD in .env differs from the stored access password. It is only used on the "
                    "very first start; to apply it run: python -m mg_archive_bot.tools.reset_password "
                    "(or use /setpassword in Telegram)."
                )
        except user_service.UserError as exc:
            log.error("%s", exc)
            return 2
        for demoted in user_service.reconcile_super_admin(session, settings.super_admin_telegram_id):
            log.warning("User %s held SUPER_ADMIN but is not SUPER_ADMIN_TELEGRAM_ID; demoted to Team Lead", demoted)
        adopted, missing = project_service.backfill_leads(session)
        if adopted:
            log.info("Adopted the creator as lead for %d project(s)", adopted)
        if missing:
            log.warning("Projects without a lead (set one via /projects → Project lead): %s", ", ".join(missing[:10]))
        if user_service.placeholder_password_in_use(session):
            log.error(
                "The stored access password is a well-known placeholder. Set a real INITIAL_ACCESS_PASSWORD in .env and run "
                "`python -m mg_archive_bot.tools.reset_password` before starting the bot."
            )
            return 2
    try:
        drive = build_drive_client(settings)
        check_drive_root(settings, drive, log)
        sheets = build_sheets_client(settings) if settings.tracking_sheet_enabled else None
    except (DriveError, OSError, ValueError) as exc:
        log.error("Google Drive setup failed: %s", exc)
        return 2

    app = build_application(settings, drive, sheets)
    log.info("Starting long polling…")
    try:
        app.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
    except InvalidToken:
        log.error("TELEGRAM_BOT_TOKEN was rejected by Telegram. Copy the token from @BotFather into .env.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
