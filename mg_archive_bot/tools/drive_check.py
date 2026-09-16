"""Verify the bot's Google Drive access before starting it.

Usage:  python -m mg_archive_bot.tools.drive_check

Loads the credentials from .env, looks up DRIVE_ROOT_FOLDER_ID, reports whether it lives in a Shared Drive,
lists its contents and creates + deletes a throw-away folder to prove the bot can write there.
Every failure is translated into the exact thing to ask the Shared Drive manager or Workspace admin for.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from ..config import Settings
from ..services.drive import DriveError, build_drive_client

CHECK_FOLDER = "_mg_bot_access_check (safe to delete)"


def _identity(settings: Settings) -> str:
    if settings.google_auth_mode == "service_account":
        try:
            data = json.loads(settings.google_service_account_file.read_text())
            return data.get("client_email", "<unknown service account>")
        except (OSError, ValueError):
            return "<unreadable service account file>"
    if settings.google_auth_mode == "oauth":
        return f"the Google account that authorised {settings.google_oauth_token_file}"
    return "fake"


def main() -> int:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print("Configuration error (check your .env):")
        for err in exc.errors():
            print(f"  - {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}")
        return 2
    problems = settings.validate_runtime()
    if problems:
        for p in problems:
            print(f"✗ {p}")
        return 2
    if settings.google_auth_mode == "fake":
        print("GOOGLE_AUTH_MODE=fake — nothing to check (no Google access is used).")
        return 0

    identity = _identity(settings)
    print(f"Google identity used by the bot: {identity}")
    try:
        drive = build_drive_client(settings)
    except (DriveError, OSError, ValueError) as exc:
        print(f"✗ Could not load Google credentials: {exc}")
        return 1

    try:
        root = drive.get_file(settings.drive_root_folder_id)
    except DriveError as exc:
        print(f"✗ Google Drive request failed: {exc}")
        print("  If this mentions 'accessNotConfigured' or 'has not been used', enable the Google Drive API in the Cloud project.")
        return 1
    if root is None:
        print(f"✗ DRIVE_ROOT_FOLDER_ID={settings.drive_root_folder_id} is not visible to {identity}.")
        print("  Ask a Manager of the Shared Drive to add that e-mail as a member (Content manager),")
        print("  or to share the root folder with it. Then run this check again.")
        return 1
    if not root.is_folder:
        print(f"✗ DRIVE_ROOT_FOLDER_ID points at '{root.name}', which is not a folder.")
        return 1
    print(f"✓ Root folder found: '{root.name}'")

    if root.drive_id:
        print(f"✓ It lives in a Shared Drive ({root.drive_id}) — files created by the bot will belong to the organisation.")
    else:
        print("! The folder is in a personal My Drive, not a Shared Drive.")
        if settings.google_auth_mode == "service_account":
            print("  Folders the bot creates would be owned by the service account. A Shared Drive is strongly recommended.")

    try:
        children = drive.list_children(root.id)
        print(f"✓ Listing works: {len(children)} item(s) in the root folder")
    except DriveError as exc:
        print(f"✗ Listing the root folder failed: {exc}")
        return 1

    try:
        test = drive.create_folder(CHECK_FOLDER, root.id)
    except DriveError as exc:
        print(f"✗ Creating a folder failed: {exc}")
        print(f"  {identity} needs 'Content manager' (or at least 'Contributor') on the root folder or the Shared Drive.")
        return 1
    print("✓ The bot can create folders here.")
    try:
        drive.delete(test.id)
        print(f"✓ The bot can move files to the trash (the throw-away folder '{CHECK_FOLDER}' is now in the trash).")
    except DriveError as exc:
        print(f"! Could not trash the throw-away folder ({exc}). Delete '{CHECK_FOLDER}' by hand.")
        print("  The bot uses this to replace outdated previews; with 'Contributor' access old previews will pile up instead.")

    print("\nAll good — you can start the bot with: python -m mg_archive_bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
