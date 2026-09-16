from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mg_archive_bot.config import Settings
from mg_archive_bot.db import init_db, session_scope
from mg_archive_bot.services import users as user_service
from mg_archive_bot.services.drive import InMemoryDriveClient

SUPER_ADMIN_ID = 1000
PASSWORD = "secret-pass-123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123:TEST",
        super_admin_telegram_id=SUPER_ADMIN_ID,
        google_auth_mode="fake",
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        work_dir=tmp_path / "work",
        initial_access_password=PASSWORD,
        timezone="Asia/Singapore",
        _env_file=None,
    )


@pytest.fixture
def db(settings: Settings):
    engine = init_db(settings.database_url)
    with session_scope() as session:
        user_service.seed_password_if_missing(session, settings.initial_access_password)
    yield engine
    engine.dispose()


@pytest.fixture
def drive() -> InMemoryDriveClient:
    return InMemoryDriveClient()


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> Path:
    """A stand-in ffmpeg that writes (10_000_000 / crf) zero bytes to the output path."""
    script = tmp_path / "ffmpeg"
    script.write_text(
        "#!/bin/sh\n"
        "crf=26; prev=''; out=''\n"
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-crf" ]; then crf="$a"; fi\n'
        '  prev="$a"; out="$a"\n'
        "done\n"
        'if [ -n "$FAKE_FFMPEG_FAIL" ]; then echo "boom" >&2; exit 1; fi\n'
        'if [ -n "$FAKE_FFMPEG_SLEEP" ]; then sleep "$FAKE_FFMPEG_SLEEP"; fi\n'
        'head -c $((10000000 / crf)) /dev/zero > "$out"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("TELEGRAM_", "GOOGLE_", "DRIVE_", "SUPER_ADMIN", "INITIAL_ACCESS", "DATABASE_URL", "FAKE_FFMPEG")):
            monkeypatch.delenv(key, raising=False)
