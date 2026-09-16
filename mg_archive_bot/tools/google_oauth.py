"""One-off helper for GOOGLE_AUTH_MODE=oauth: obtain a refreshable user token for Google Drive.

Usage:  python -m mg_archive_bot.tools.google_oauth
Requires an OAuth "Desktop app" client secrets JSON (GOOGLE_OAUTH_CLIENT_SECRETS_FILE) and the
`google-auth-oauthlib` package. Opens a browser for consent and writes GOOGLE_OAUTH_TOKEN_FILE.
"""
from __future__ import annotations

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
    settings.google_oauth_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.google_oauth_token_file.write_text(creds.to_json())
    print(f"Token saved to {settings.google_oauth_token_file}. Set GOOGLE_AUTH_MODE=oauth in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
