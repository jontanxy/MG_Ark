from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str = Field(min_length=1)
    super_admin_telegram_id: int

    # Access
    initial_access_password: str = ""
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    provisioning_token_ttl_hours: int = 24
    unauthorised_group_leave_minutes: int = 60
    # Abuse guards
    unregistered_reply_interval_seconds: int = 60  # reply to an unknown sender at most once per interval
    password_breaker_failures: int = 30  # global wrong-password budget...
    password_breaker_window_minutes: int = 10  # ...inside this window...
    password_breaker_pause_minutes: int = 15  # ...pauses registration for this long
    status_cooldown_seconds: int = 60  # group /status re-uses the last check inside this window

    # Storage
    database_url: str = "sqlite:///data/mg_archive.sqlite3"
    work_dir: Path = Path("data/work")

    # Google Drive
    google_auth_mode: Literal["service_account", "oauth", "fake"] = "service_account"
    google_service_account_file: Path = Path("secrets/service-account.json")
    google_oauth_client_secrets_file: Path = Path("secrets/oauth-client.json")
    google_oauth_token_file: Path = Path("secrets/oauth-token.json")
    drive_root_folder_id: str = ""

    # Folder names (defaults follow the requirements exactly)
    folder_name_working_file: str = ""
    folder_name_fonts: str = ""
    folder_name_ae: str = ""
    folder_name_psd: str = ""
    folder_name_final_render: str = ""
    folder_name_timeline: str = ""
    folder_name_contin_videos: str = ""
    folder_name_contin_lyrics: str = ""
    folder_name_prores: str = ""
    folder_name_hap: str = ""
    folder_name_png: str = ""
    folder_name_previews: str = ""

    # Previews
    ffmpeg_path: str = "ffmpeg"
    preview_max_width: int = 1280
    preview_crf: int = 26
    previews_enabled: bool = True

    # Scheduling
    scan_interval_minutes: int = 30
    reminder_hour: int = 10
    reminder_min_gap_hours: int = 20
    timezone: str = ""  # IANA name; empty = host local timezone

    search_page_size: int = 5
    log_level: str = "INFO"
    log_file: Path | None = Path("data/bot.log")  # rotating log file; empty value disables

    @field_validator("drive_root_folder_id", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("log_file", mode="before")
    @classmethod
    def _optional_path(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("reminder_hour")
    @classmethod
    def _hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("REMINDER_HOUR must be 0-23")
        return v

    def folder_names(self) -> dict[str, str]:
        return {
            "working_file": self.folder_name_working_file,
            "fonts": self.folder_name_fonts,
            "ae": self.folder_name_ae,
            "psd": self.folder_name_psd,
            "final_render": self.folder_name_final_render,
            "timeline": self.folder_name_timeline,
            "contin_videos": self.folder_name_contin_videos,
            "contin_lyrics": self.folder_name_contin_lyrics,
            "prores": self.folder_name_prores,
            "hap": self.folder_name_hap,
            "png": self.folder_name_png,
            "previews": self.folder_name_previews,
        }

    def validate_runtime(self) -> list[str]:
        """Return human-readable configuration problems (empty when OK)."""
        problems: list[str] = []
        if self.google_auth_mode == "service_account" and not self.google_service_account_file.exists():
            problems.append(f"GOOGLE_SERVICE_ACCOUNT_FILE not found: {self.google_service_account_file}")
        if self.google_auth_mode == "oauth" and not self.google_oauth_token_file.exists():
            problems.append(
                f"GOOGLE_OAUTH_TOKEN_FILE not found: {self.google_oauth_token_file} "
                "(run: python -m mg_archive_bot.tools.google_oauth)"
            )
        if self.google_auth_mode != "fake" and not self.drive_root_folder_id:
            problems.append("DRIVE_ROOT_FOLDER_ID is required")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
