# MG Archive Bot — Design

Source requirements: `docs/REQUIREMENTS.md` (Motion Graphics Archive Management System).

## 1. Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ (developed on 3.14) | Mature Telegram + Google client libraries |
| Telegram | `python-telegram-bot` 22.8 (async, job-queue extra) | Conversation handlers, inline keyboards, job queue |
| Database | SQLite via SQLAlchemy 2.0 (file `data/mg_archive.sqlite3`) | Zero-ops, single process; swap URL for Postgres later |
| Google Drive | Drive API v3 (`google-api-python-client`) | Folder creation, listing, download/upload |
| Drive auth | Service account (recommended, shared into a Shared Drive folder as Content manager) **or** OAuth user token (for a plain Google account) | Service accounts cannot own files in a personal My Drive; OAuth mode covers non-Workspace accounts |
| Previews | `ffmpeg` (H.264 MP4, ≤1280px wide, faststart), stored on Drive, delivered as links | ProRes 4444 → small MP4 that streams in Drive's player |
| Config | `pydantic-settings` reading `.env` | Typed, validated config |
| Tests | `pytest` + `pytest-asyncio`, in-memory SQLite, fake Drive | Deterministic |

The bot is a single long-running process: Telegram long-polling + APScheduler jobs + one background preview worker.

## 2. Package layout

```
mg_archive_bot/
  __main__.py            python -m mg_archive_bot
  config.py              Settings (env)
  constants.py           enums (Role, UserStatus, ProjectStatus, AssetCategory), folder tree spec
  db.py                  engine + session factory + init
  models.py              SQLAlchemy models
  security.py            scrypt password hashing, provisioning tokens
  services/
    drive.py             DriveClient protocol + GoogleDriveClient (sync, run in threads)
    users.py             registration, roles, revoke/restore, login lockout
    groups.py            MG groups + provisioning tokens
    projects.py          project creation, folder tree, metadata, assignments, state transitions
    validation.py        Drive-is-truth validation → report + status transition
    search.py            term normalisation, AND matching, ranking
    previews.py          preview planning, ffmpeg transcode, upload, Telegram file_id cache
    notifications.py     message builders for group announcements / reminders / completion
  bot/
    app.py               build Application, register handlers + jobs
    access.py            authorisation decorators, group gate
    keyboards.py         inline keyboards
    formatting.py        HTML formatting helpers
    jobs.py              scan job, reminder job, preview worker
    handlers/
      auth.py            /start + password conversation
      admin.py           super-admin commands
      mg_groups.py       /creategroup, /activate, bot-added-to-group handling
      projects.py        /newproject wizard, /projects, project actions, metadata edit, assignment
      search.py          /search, result buttons, preview sending
      group_mode.py      group-only commands: /status, /remind
      common.py          /help, /whoami, /cancel, error handler
  tools/
    google_oauth.py      one-off OAuth token bootstrap (OAuth mode only)
tests/
```

## 3. Data model

* **users**: `telegram_id` PK, `name`, `username`, `role` (SUPER_ADMIN|TEAM_LEAD|DESIGNER), `status` (ACTIVE|REVOKED), timestamps.
* **login_attempts**: `telegram_id` PK, `failed_count`, `locked_until` — password brute-force lockout (5 failures → 15 min).
* **settings**: key/value — `access_password_hash` (scrypt, salted).
* **mg_groups**: `chat_id` PK (re-keyed in place on basic-group → supergroup migration), `title`, `status` (ACTIVE|REVOKED), `created_by`, `authorised_at`, `revoked_by` (Super Admin id for a deliberate revoke; NULL when the bot was simply removed from the chat).
* **provisioning_tokens**: `token` unique (`MG-XXXX-XXXX`), `created_by`, `expires_at` (24 h), `used_at`, `used_chat_id`.
* **projects**: `id`, `name`, `status`, declarations `has_timeline`, `has_contin_videos`, `has_contin_lyrics`, `has_psd`, metadata (`collection`, `description`, `event`, `ministry`, `style`, `colours`, `year`, `creator`, `asset_types`), `created_by`, `mg_group_chat_id` (nullable), `drive_root_id`, `drive_link`, `last_validated_at`, `last_complete` (bool), `last_reminder_at`, `verified_by`, `verified_at`, timestamps.
* **collections**: `id`, `name` (unique, case-insensitive), `drive_id`/`link` (folder under the root, created lazily when the first sub-project is provisioned; an existing same-named folder is re-used), `created_by`. `projects.collection_id` → sub-projects live in `<root>/<Collection>/<Project>/…`; their `collection` metadata mirrors the folder name and is not editable separately. Project names are unique within a collection (or within the top level). Display name is `Collection / Project`.
* **tags** + **project_tags**: normalised lowercase tag names.
* **project_folders**: `project_id`, `key` (e.g. `timeline_prores`), `name`, `drive_id`, `link`.
* **assignments**: `project_id`, `user_id`, `category` (ALL|WORKING_FILE|TIMELINE|CONTIN_VIDEOS|CONTIN_LYRICS|PSD).
* **preview_assets**: `project_id`, `category`, `source_key`, `source_drive_id`, `source_name`, `source_fingerprint`, `preview_drive_id`, `preview_link`, `preview_name`, `size_bytes`, `status` (PENDING|READY|FAILED), `error`.
* **validation_runs**: `project_id`, `run_at`, `complete`, `report_json` (audit trail).

## 4. Folder tree (always created)

```
<Project Name>/
  Working File/            key working_file
    Fonts/                 key fonts        (required)
    AE/                    key ae           (required)
    PSD/                   key psd          (ONLY created when has_psd = true)
  Final Render/            key final_render
    Timeline/              key timeline
      ProRes 4444/         key timeline_prores      (required iff has_timeline)
      Hap/Hap Alpha/       key timeline_hap         (required iff has_timeline)
    Contin Videos/         key contin_prores/contin_hap (required iff has_contin_videos)
    Contin Lyrics/
      PNG/                 key lyrics_png           (required iff has_contin_lyrics)
  _Previews/               key previews (bot-managed MP4 previews; not part of the spec tree)
```

Folder names are exactly as in the requirements (`Hap/Hap Alpha` is one folder name; Drive allows `/`). All names are overridable in `.env` (`FOLDER_NAME_*`).

The project root is created inside `DRIVE_ROOT_FOLDER_ID` (a folder in a Shared Drive or My Drive). Duplicate project names get ` (2)`, ` (3)` suffixes on Drive and are rejected in the bot if an active project with the same name exists.

## 5. Validation (Drive is the source of truth)

A *leaf* is satisfied when it contains ≥1 non-folder, non-trashed file (searched recursively up to depth 3 so designers can upload sub-folders).

Required leaves: `fonts`, `ae` always; `timeline_prores`, `timeline_hap` iff `has_timeline`; `contin_prores`, `contin_hap` iff `has_contin_videos`; `lyrics_png` iff `has_contin_lyrics`; `psd` iff `has_psd`.

Result → `ValidationReport(items=[{key,label,category,required,file_count,ok,error}], complete)`. Stored in `validation_runs`.

A leaf that *cannot be listed* (Drive outage, expired credentials, deleted folder) is marked `error`, not empty: the scan
then changes nothing (no status transition, no stored run, no READY notice, no reminder) and the progress view shows
"⚠️ could not check". OS junk (`.DS_Store`, `._*`, `Thumbs.db`, 0-byte files) never counts as an upload.

State machine:

```
DRAFT ──(wizard confirmed, folders created)──▶ ACTIVE
ACTIVE / INCOMPLETE / READY_FOR_VERIFICATION ──(scan: not complete)──▶ INCOMPLETE
ACTIVE / INCOMPLETE ──(scan: complete)──▶ READY_FOR_VERIFICATION   (+ group + team-lead notice)
READY_FOR_VERIFICATION ──(Team Lead "Verify")──▶ ARCHIVED           (+ group notice)
ARCHIVED ──(Team Lead "Reopen")──▶ ACTIVE
```
ARCHIVED projects are not rescanned automatically. Verify is only offered when the latest scan is complete (a fresh scan is run first).

Scans: every `SCAN_INTERVAL_MINUTES` (default 30) for all ACTIVE/INCOMPLETE/READY projects; on demand from the project menu (`Check progress`) and group `/status`.

## 6. Previews

* Sources: `timeline_prores` (iff has_timeline) and `contin_prores` (iff has_contin_videos); video files by MIME `video/*` or extension `.mov .mxf .mp4 .m4v .avi`.
* Plan: for each source file, a preview exists if `(source_drive_id, source_md5 or modifiedTime)` matches a READY row. New/changed → enqueue. FAILED rows are **not** retried by scheduled scans (multi-GB downloads); the Team Lead's *Generate previews* forces a retry. Unattended failures are DM'd to the project creator. Before downloading, free disk in `WORK_DIR` must exceed 1.3 × source size + 200 MB.
* Worker (single background task, serialised): download → `ffmpeg -i in -vf "scale=w='trunc(min(1280,iw)/2)*2':h=-2,format=yuv420p" -c:v libx264 -preset medium -crf 26 -movflags +faststart -c:a aac -b:a 128k out.mp4` → upload the MP4 to `_Previews/` (name `<source stem>.mp4`) → store the row with its Drive link. The source download is deleted before the upload.
* Delivery: **Drive links only.** The bot never uploads video to Telegram (no 50 MB limit, no local cache): *Preview* replies with a URL button that opens the MP4 in Google Drive's player. Statuses are PENDING / READY / FAILED.
* Drive file names are never used as local path components (Drive allows `/` and `..` in names); downloads go to a fixed name inside a per-job work directory. Previews whose source disappeared are deleted from `_Previews/`. On shutdown running ffmpeg processes are killed.
* Triggers: after each scan (only declared categories), and `Generate previews` from the project menu. Progress/failure is reported to the requesting Team Lead privately.

## 7. Search (private chat only)

`/search worship, gold, particles` → terms split on commas (fallback: whitespace when no comma), trimmed, lower-cased, de-duplicated, empty removed. Every term must match at least one field (AND).

Each term scores by its *best* tier: exact tag 10⁸ · project name 10⁶ · event/collection 10⁴ · other metadata (ministry, style, colours, year, creator, asset types; partial tag) 10² · description 1. Because tiers are separated by more than the term cap (20), one exact-tag match always outranks any number of lower-tier matches — the ranking order of §10 is strict. Ties: whole-name match first, then year desc, then name. Paginated 5 per page. DRAFT projects are excluded.

Each result:
```
🎬 Easter Opening 2026
#worship #gold #particles
3 previews available · READY_FOR_VERIFICATION
[Preview] [Open Archive] [Details]
```
`Preview`: one preview → message with a "Play preview on Google Drive" URL button; several → one URL button per preview. `Open Archive`: URL button to the Drive folder. `Details`: full metadata + folder links + latest validation summary.

## 8. Roles and authorisation

| Capability | Super Admin | Team Lead | Designer |
|---|---|---|---|
| /search, preview, open archive | ✔ | ✔ | ✔ |
| /newproject, /projects, project menu (validate, announce, remind, assign, metadata, previews, verify, reopen) | ✔ | ✔ | ✖ |
| /creategroup (provisioning token) | ✔ | ✔ | ✖ |
| /setpassword, /users, /groups, revoke/restore user, set role, revoke group | ✔ | ✖ | ✖ |

* Super Admin = `SUPER_ADMIN_TELEGRAM_ID` from `.env`, auto-registered, cannot be revoked/demoted.
* Every private-chat handler requires an ACTIVE user (except /start and the password step).
* Every group handler requires **both** an ACTIVE user **and** an ACTIVE MG group (chat_id in `mg_groups`). Unauthorised groups get one short denial per command; revoked groups are left.
* Group chat allows only: `/status`, `/remind` (Team Lead), `/activate <token>`, `/help`. Search/preview/project/user management commands in groups reply "private chat only" once.
* The group gate exempts `/activate` (it still requires an ACTIVE Team Lead / Super Admin) and the `my_chat_member` / migration service updates.
* Basic group → supergroup migration (chat id changes) is handled twice: a `StatusUpdate.MIGRATE` handler re-keys `mg_groups.chat_id` and `projects.mg_group_chat_id`, and every group send catches `ChatMigrated`, re-keys, and retries.
* A group revoked by the Super Admin cannot be re-activated with a Team Lead token; the Super Admin restores it from `/groups`. A group the bot was merely removed from can be re-activated with a fresh token.

## 9. Authentication flow

`/start` (private): known ACTIVE → menu · REVOKED → denied (password cannot re-register) · unknown → "Enter access password". Password message is deleted after reading (best effort). Correct → user created as DESIGNER/ACTIVE. Wrong → count; after 5 failures lock for 15 min. Password hash = scrypt (stdlib) with random salt. Initial password from `INITIAL_ACCESS_PASSWORD`, only used to seed the DB once; `/setpassword` (Super Admin) rotates it.

## 10. MG Group provisioning

1. Team Lead `/creategroup` in private → token `MG-XXXX-XXXX`, valid 24 h, single use.
2. Team Lead creates the Telegram group and adds the bot.
3. Bot receives `my_chat_member` (added). If the adder has exactly one unused token → auto-authorise. Otherwise bot asks for `/activate MG-XXXX-XXXX` in the group (any Team Lead/Super Admin who is ACTIVE can run it).
4. Group stored as ACTIVE; token marked used. Bot posts a short confirmation.
5. Super Admin `/groups` → revoke; bot leaves the chat. `/groups` also offers *Restore* for revoked groups.

Bot in a chat with no authorisation and no valid token within `UNAUTHORISED_GROUP_LEAVE_MINUTES` (default 60) leaves the chat (job).

## 11. Project creation wizard (`/newproject`, private, Team Lead)

0. Location: top level, an existing collection, or a new collection name (folder under the root).
1. Name (text, 2–100 chars, unique within that location).
2. Asset declaration: inline toggles Timeline / Contin Videos / Contin Lyrics / PSD, then "Continue".
3. MG group: auto if exactly one ACTIVE group; else pick (or "none — announce later").
4. Optional metadata (event, collection, ministry, style, colours, tags, description) via "Add metadata now" / "Skip". Year defaults to current year; creator defaults to creator's name; asset types derived.
5. Assign designers: multi-select of ACTIVE users (category ALL); refine per-category later from project menu.
6. Confirm → folders created on Drive (in a worker thread) → project ACTIVE → announcement posted to the MG group (folder links, required assets, assigned designers) → Team Lead gets the link summary.

## 12. Group messages

* **Announcement** (on creation / "Announce"): project name, Drive root link, per-folder links for required leaves, assigned designers (mentions).
* **Progress / reminder** (`/status`, daily reminder job at `REMINDER_HOUR` local time, "Remind" button): ✅/❌ per required leaf, missing items with responsible designers mentioned. Reminder job skips projects reminded within the last 20 h and projects that are complete/archived.
* **Ready for verification**: posted once when a scan flips the project to READY.
* **Archived**: posted when the Team Lead verifies.

## 12a. Startup checks

* Password seeded once from `INITIAL_ACCESS_PASSWORD`; any DB row holding SUPER_ADMIN that is not `SUPER_ADMIN_TELEGRAM_ID` is demoted.
* The Drive root folder is fetched: unreachable → exit with a sharing hint; service-account + My Drive logs a loud ownership/quota warning. Listings use `corpora=allDrives`, so the identity may be a Shared Drive member *or* merely have the root folder shared with it. Deletions are "move to trash" because Content managers cannot permanently delete on Shared Drives.
* Leftovers in `WORK_DIR` are purged; PENDING previews are re-planned by the next scan.
* The bot never changes Drive permissions: access to archives comes from Shared Drive membership or from sharing the root folder with the team's Google accounts.
* Login lockouts are reported to the Super Admin by DM. A global breaker (30 wrong passwords / 10 min across all accounts) pauses registration for 15 min and alerts once. Unknown senders are answered at most once per minute; group `/status` re-uses a check younger than 60 s instead of hitting Drive again.

## 13. Config (`.env`)

```
TELEGRAM_BOT_TOKEN=
SUPER_ADMIN_TELEGRAM_ID=
INITIAL_ACCESS_PASSWORD=
DATABASE_URL=sqlite:///data/mg_archive.sqlite3
GOOGLE_AUTH_MODE=service_account        # or oauth
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/service-account.json
GOOGLE_OAUTH_CLIENT_SECRETS_FILE=secrets/oauth-client.json
GOOGLE_OAUTH_TOKEN_FILE=secrets/oauth-token.json
DRIVE_ROOT_FOLDER_ID=                   # shared with the bot's identity as Content manager (folder-level sharing is enough)
FFMPEG_PATH=ffmpeg
WORK_DIR=data/work
SCAN_INTERVAL_MINUTES=30
REMINDER_HOUR=10
TIMEZONE=Asia/Singapore
PROVISIONING_TOKEN_TTL_HOURS=24
```

## 14. Concurrency

* The application runs with `concurrent_updates(True)` so one user's Drive scan never queues other users' updates.
* Rule for handlers: **commit before awaiting**. Every DB mutation is committed before the next Telegram/Drive call, so
  no SQLite write lock is ever held across an `await` (a second writer would otherwise spin on the event loop).
* Rule for buttons: answer each callback query exactly once (Telegram rejects a second answer).
* Non-idempotent actions (create archive, verify, reopen, scans) run under the per-project lock and refresh the ORM
  object after acquiring it, so a double tap or an overlapping scan cannot repeat a transition.
* Any command or button press cancels a pending free-text step; pending steps expire after 30 minutes.
* `sqlite:///:memory:` is rejected (one shared connection would break session isolation); tests use temp files.
* Drive + ffmpeg calls are blocking; every call goes through `asyncio.to_thread`. The Drive client keeps a thread-local service object (httplib2 is not thread-safe). Sessions live on the event loop only; threads receive plain data.
* Scans for different projects may run concurrently; per-project scans are serialised by an asyncio lock keyed by project id.
* Preview worker is a single asyncio task consuming an `asyncio.Queue`; enqueue is idempotent per source file.
