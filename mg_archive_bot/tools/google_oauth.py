"""One-off helper for GOOGLE_AUTH_MODE=oauth: obtain a refreshable user token for Google Drive.

Usage:  python -m mg_archive_bot.tools.google_oauth
Requires an OAuth "Desktop app" client secrets JSON (GOOGLE_OAUTH_CLIENT_SECRETS_FILE) and the
`google-auth-oauthlib` package. Opens a browser for consent and writes GOOGLE_OAUTH_TOKEN_FILE.
"""
from __future__ import annotations

import os
import sys

from ..config import Settings
from ..services.drive import DRIVE_SCOPES


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the OAuth helper first:  pip install google-auth-oauthlib", file=sys.stderr)
        return 2
    if not settings.google_oauth_client_secrets_file.exists():
        print(f"Client secrets file not found: {settings.google_oauth_client_secrets_file}", file=sys.stderr)
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_oauth_client_secrets_file), DRIVE_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    os.umask(0o077)
    settings.google_oauth_token_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.google_oauth_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(settings.google_oauth_token_file, 0o600)
    print(f"Token saved to {settings.google_oauth_token_file}. Set GOOGLE_AUTH_MODE=oauth in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
