# MG Archive Bot

A Telegram bot that turns the Motion Graphics ministry's Google Drive archive into a managed, searchable asset library.
It creates the standard project folder tree on Drive, announces archive requests in the MG Group, checks Google Drive
automatically to track what has been uploaded, reminds designers about missing files, generates MP4 previews from ProRes
masters, and lets everyone search and preview past projects privately.

Requirements: `docs/REQUIREMENTS.md` · Design: `docs/DESIGN.md`

## Features

| Area | What the bot does |
|---|---|
| Archive creation | `/newproject` wizard → declares required assets → creates `Working File/…` and `Final Render/…` on Google Drive → announces links in the MG Group |
| Progress tracking | Google Drive is the source of truth. Scheduled scans validate only the declared assets; status moves ACTIVE → INCOMPLETE / READY_FOR_VERIFICATION → ARCHIVED (Team Lead verification) |
| Reminders | Daily reminder in the MG Group listing missing folders and mentioning the responsible designers; on-demand `/status` and `/remind` |
| Previews | ProRes 4444 files in Timeline / Contin Videos are transcoded to small MP4s (ffmpeg) stored in `_Previews/` on Drive; **Preview** hands out their Drive links privately (nothing is uploaded to Telegram) |
| Discovery | `/search worship, gold, particles` (AND, case-insensitive, de-duplicated) ranked: exact tag → name → event/collection → metadata → description. Result cards offer **Preview**, **Open Archive**, **Details** |
| Roles | Super Admin (fixed Telegram ID), Team Lead, Designer — exactly the permissions in the requirements |
| Access | Password registration on `/start` (never asked again), revocation, brute-force lockout, MG Groups authorised only via provisioning tokens, "authorised user AND authorised group" rule in groups |

## 1. Prerequisites

* Python 3.11+ (developed and tested on 3.14) or Docker
* `ffmpeg` on the machine running the bot (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu)
* A Telegram bot token from [@BotFather](https://t.me/BotFather)
* Your numeric Telegram user id (send `/start` to [@userinfobot](https://t.me/userinfobot)) — this account becomes the Super Admin
* Google Drive access for the bot (see §3)

The bot must run on an always-on machine (Mac mini, VPS, NAS, Docker host). Preview generation downloads the ProRes
masters, so give it disk space (`WORK_DIR`) and a reasonable connection.

## 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set `TELEGRAM_BOT_TOKEN`, `SUPER_ADMIN_TELEGRAM_ID`, `INITIAL_ACCESS_PASSWORD`, and the Google settings from §3.

## 3. Google Drive access

Two supported modes. Pick one.

### Option A — Service account + Shared Drive (recommended for Google Workspace)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project, enable the **Google Drive API**.
2. IAM & Admin → Service Accounts → create one → Keys → **Add key (JSON)**. Save it as `secrets/service-account.json`.
3. Share the archive with the service account's e-mail (`…@….iam.gserviceaccount.com`) as **Content manager** —
   either as a member of the whole Shared Drive, or just on the root folder (Share → add the e-mail). Folder-level
   sharing is enough: the bot only touches that folder and what it creates inside it.
4. Copy the root folder's id from its URL (`https://drive.google.com/drive/folders/<THIS-PART>`) into
   `DRIVE_ROOT_FOLDER_ID`.

Why a Shared Drive: files the bot creates in a personal "My Drive" would be owned by the robot account and count against
its quota; in a Shared Drive everything is owned by the ministry. The bot never changes sharing settings — designers see
the archive because they are members of the Shared Drive (or because the root folder was shared with them).

### If you cannot open the Google Cloud Console with your work account

The Cloud project and the Shared Drive are unrelated. You do **not** need to own the Shared Drive, and the Cloud
project does not have to belong to the church's Workspace:

1. Sign in to [console.cloud.google.com](https://console.cloud.google.com/) with **any** Google account you control
   (a personal Gmail account is fine), create a project, enable the Google Drive API, create the service account and
   download its JSON key (steps 1–2 above). No billing account is needed.
2. Copy the service account's e-mail address (`…@….iam.gserviceaccount.com`).
3. Share the archive with that e-mail as **Content manager**. If you can share folders in the Shared Drive yourself,
   sharing just the root folder is enough (Share → add the e-mail → Content manager). Otherwise ask a **Manager** of the
   Shared Drive to add it as a member.
4. Put the id of the root folder into `DRIVE_ROOT_FOLDER_ID` and verify everything with:

   ```bash
   python -m mg_archive_bot.tools.drive_check
   ```

   The check prints which e-mail the bot uses, whether the folder is visible, whether it is in a Shared Drive, and
   whether the bot can create folders there — and tells you exactly what to ask for if something is missing.

If the Manager cannot add the service account ("external members are not allowed"), the Workspace admin has restricted
Shared Drives to organisation accounts. Then ask the admin for one of: allow the service account as an external member,
create the Cloud project and service account inside the organisation and give you the JSON key, or enable Google Cloud
for your work account so you can do steps 1–2 there.

### Option B — OAuth with a normal Google account (no Workspace)

1. Cloud Console → APIs & Services → Credentials → **Create OAuth client ID** → *Desktop app*. Download the JSON as
   `secrets/oauth-client.json`. On the OAuth consent screen either choose user type **Internal** (Workspace) or set the
   publishing status to **In production** — tokens issued while the app is in *Testing* expire after 7 days and the bot
   would stop working with a "credentials could not be refreshed" error.
2. `pip install google-auth-oauthlib` then run `python -m mg_archive_bot.tools.google_oauth` on a machine with a browser,
   sign in with the account that owns the archive, and copy the produced `secrets/oauth-token.json` to the bot host.
3. Set `GOOGLE_AUTH_MODE=oauth` and `DRIVE_ROOT_FOLDER_ID`.

### Option C — Try it without Google

`GOOGLE_AUTH_MODE=fake` runs the whole bot against an in-memory Drive: every flow works in Telegram, nothing is written to
Google. Useful for a first test of the wizard, groups and roles.

## 4. Run

```bash
source .venv/bin/activate
python -m mg_archive_bot
```

Or with Docker (ffmpeg included):

```bash
docker compose up -d --build
```

On first start the access password is seeded from `INITIAL_ACCESS_PASSWORD`; afterwards change it with `/setpassword`.

## 5. First-time setup in Telegram

1. **Super Admin**: open the bot, send `/start`. You are registered automatically with the Super Admin role.
2. **Team Leads**: each sends `/start` and the access password (they register as Designer), then the Super Admin runs
   `/users` → taps the user → **Make Team Lead**.
3. **Designers**: `/start` + password. Done.
4. **MG Group**: a Team Lead runs `/creategroup` (private chat) to get a token, creates the Telegram group, adds the bot.
   If the token holder added the bot the group is authorised automatically; otherwise send `/activate MG-XXXX-XXXX`
   in the group. Only groups authorised this way are usable.

## 6. Day-to-day

**Team Lead (private chat)**

* `/newproject` — where it lives (top level, an existing collection, or a new collection such as `BF`) → name →
  toggle Timeline / Contin Videos / Contin Lyrics / PSD → (pick MG Group) → optional metadata → assign designers →
  **Create archive**. Folders are created and the announcement with folder links is posted.
* **Collections** group sub-projects in one folder: `BF/Opening/…`, `BF/Worship/…`. Each sub-project keeps its own
  declarations, tracking, previews and verification; its Collection metadata is set automatically, so `/search bf`
  finds all of them. A collection folder that already exists under the root is re-used.
* `/projects` → project menu: **Check progress**, **Announce**, **Remind**, **Assign designers** (per folder group),
  **Edit metadata**, **Declared assets**, **MG group**, **Generate previews**, **Previews**, **Verify & archive**,
  **Reopen**, **Open in Google Drive**.

**Everyone (private chat)**

* `/search worship, gold` or simply type keywords. Each result has **Preview**, **Open Archive**, **Details**.

**MG Group**

* `/status` — progress of this group's open archives (fresh Drive check).
* `/remind` — Team Lead only; posts reminders for missing uploads.
* Everything else (search, previews, project and user management) is refused in groups to keep the chat clean.

**Super Admin (private chat)**

* `/users` (roles, revoke, restore), `/groups` (revoke → bot leaves the group; restore), `/setpassword`.
* The Super Admin is also DM'd whenever someone gets locked out for repeated wrong passwords.

## 7. How validation and previews work

* Every `SCAN_INTERVAL_MINUTES` (default 30) and on every manual check, the bot lists each required leaf folder on Drive
  (recursively, 3 levels). A leaf is satisfied when it contains at least one file. Required leaves are
  `Working File/Fonts`, `Working File/AE` (always) and, only if declared, `Timeline/ProRes 4444`, `Timeline/Hap/Hap Alpha`,
  `Contin Videos/ProRes 4444`, `Contin Videos/Hap/Hap Alpha`, `Contin Lyrics/PNG`, `Working File/PSD`.
* When everything declared is present the project becomes **READY_FOR_VERIFICATION** and the MG Group is told once.
  A Team Lead then presses **Verify & archive** → **ARCHIVED** (announced). Removing files later makes it INCOMPLETE again.
* Reminders go out daily at `REMINDER_HOUR` (in `TIMEZONE`) for INCOMPLETE projects, at most once per
  `REMINDER_MIN_GAP_HOURS`.
* Previews: video files found in the declared `ProRes 4444` folders are downloaded, transcoded
  (`H.264, ≤1280 px wide, CRF 26, faststart`) and uploaded to `<Project>/_Previews/` on Drive. The bot never uploads video
  to Telegram: **Preview** replies with a button that opens the MP4 in Google Drive's player, which streams the small
  file instantly (the multi-GB ProRes master is never opened). Previews are regenerated when the source file changes and
  removed when it disappears.

## 8. Configuration reference

See `.env.example` — every variable is documented there. Folder names default to the requirements
(`Hap/Hap Alpha` is a single folder name; Drive allows the slash) and can be overridden with `FOLDER_NAME_*`.

## 9. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite (53 tests) covers services, validation/state machine, search ranking, preview planning and transcoding
(with a stand-in ffmpeg), jobs, and end-to-end handler flows through a fake Telegram + in-memory Drive.

## 10. Security notes

* **No inbound network exposure.** The bot uses Telegram long polling: it only makes outbound HTTPS calls to
  Telegram and Google. Nothing listens on a port, so there is nothing to scan or flood directly.
* **Secrets** live only in `.env` and `secrets/` (both git-ignored; keep them `chmod 600`). Nothing secret is ever
  logged or sent to users. The service account can only reach the Drive folder shared with it.
* **Access** requires the shared password once per Telegram account; 5 wrong attempts lock that account for
  15 minutes and alert the Super Admin. 30 wrong attempts across all accounts within 10 minutes pause registration
  for 15 minutes (one alert). Revoked accounts cannot re-register. Unknown senders get at most one reply per minute.
* **Authorisation** is checked server-side for every command and every button press (crafted button data is
  rejected), the Super Admin is pinned to `SUPER_ADMIN_TELEGRAM_ID`, and group commands need both an authorised user
  and an authorised MG Group. Unauthorised groups the bot is added to are left after an hour.
* **Input handling**: all user text is HTML-escaped, database access is parameterised, and Drive file names are never
  used as local paths.
* **What to do if something leaks**: bot token → regenerate it in @BotFather and update `.env`; service-account key →
  delete it in the Cloud Console *Keys* tab and download a new one; access password → `/setpassword` and revoke any
  unexpected users in `/users`.

## 11. Troubleshooting

The bot logs to the terminal **and** to `data/bot.log` (rotating). When something does not respond, look there first:

```bash
tail -n 50 data/bot.log
```

| Symptom | Fix |
|---|---|
| `Configuration problem: DRIVE_ROOT_FOLDER_ID is required` | Set it in `.env` (or use `GOOGLE_AUTH_MODE=fake` to test without Google) |
| `Google Drive error: … 404` when creating a project | The root folder is not shared with the service account / OAuth user — run `python -m mg_archive_bot.tools.drive_check` |
| `ffmpeg not found` in preview failure messages | Install ffmpeg or set `FFMPEG_PATH` |
| Group commands say "not an authorised MG Group" | Run `/creategroup` privately, then `/activate <token>` in the group |
| Reminders arrive at the wrong hour | Set `TIMEZONE` to your IANA zone (e.g. `Asia/Singapore`) |
| Progress shows "⚠️ could not check Google Drive" | Drive was unreachable or credentials expired; nothing changes until a scan succeeds. Check the bot log |
