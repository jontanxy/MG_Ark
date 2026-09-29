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
* **projects**: `id`, `name`, `status`, `lead_id` (the one Team Lead responsible; NULL only for legacy rows or after the lead lost the role — management then falls to the Super Admin until reassigned), declarations `has_timeline`, `has_contin_videos`, `has_contin_lyrics`, `has_titlebars`, `has_psd`, metadata (`collection`, `description`, `event`, `ministry`, `style`, `colours`, `year`, `creator`, `asset_types`), `created_by`, `mg_group_chat_id` (nullable), `drive_root_id`, `drive_link`, `last_validated_at`, `last_complete` (bool), `last_reminder_at`, `verified_by`, `verified_at`, timestamps.
* **collections**: `id`, `name` (unique, case-insensitive), `drive_id`/`link` (folder under the root, created lazily when the first sub-project is provisioned; an existing same-named folder is re-used), `created_by`. `projects.collection_id` → sub-projects live in `<root>/<Collection>/<Project>/…`; their `collection` metadata mirrors the folder name and is not editable separately. Project names are unique within a collection (or within the top level). Display name is `Collection / Project`.
* **tags** + **project_tags**: normalised lowercase tag names.
* **project_folders**: `project_id`, `key` (e.g. `timeline_prores`), `name`, `drive_id`, `link`.
* **assignments**: `project_id`, `user_id`, `category` (ALL|WORKING_FILE|TIMELINE|CONTIN_VIDEOS|CONTIN_LYRICS|TITLEBARS|PSD).
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
    Titlebars/             key titlebars            (ONLY created when has_titlebars = true; required iff has_titlebars)
  _Previews/               key previews (bot-managed MP4 previews; not part of the spec tree)
```

Folder names are exactly as in the requirements (`Hap/Hap Alpha` is one folder name; Drive allows `/`). All names are overridable in `.env` (`FOLDER_NAME_*`).

The project root is created inside `DRIVE_ROOT_FOLDER_ID` (a folder in a Shared Drive or My Drive). Duplicate project names get ` (2)`, ` (3)` suffixes on Drive and are rejected in the bot if an active project with the same name exists.

## 5. Validation (Drive is the source of truth)

A *leaf* is satisfied when it contains ≥1 non-folder, non-trashed file (searched recursively up to depth 3 so designers can upload sub-folders).

Required leaves: `fonts`, `ae` always; `timeline_prores`, `timeline_hap` iff `has_timeline`; `contin_prores`, `contin_hap` iff `has_contin_videos`; `lyrics_png` iff `has_contin_lyrics`; `titlebars` iff `has_titlebars`; `psd` iff `has_psd`.

Declarable asset types live in one place, `constants.CATEGORY_FLAGS` (category → project flag); the wizard, the
assignment list, validation, the details view and the index sheet all follow it. Titlebars were added after the
first archives existed, so their folders are created on demand like PSD: existing projects are never changed
unless a lead declares Titlebars for them. `db.upgrade_schema` adds the new column on start-up with its default in
the same statement (`ALTER TABLE … ADD COLUMN has_titlebars BOOLEAN DEFAULT 0`), so existing rows read `0` without
being rewritten (their `updated_at` is kept) and there is no second step that could be lost in a crash. The
statement is sent to the driver as written (`exec_driver_sql`), so a default may contain any character.

Folders created on demand can fail part-way (several assets switched on at once). `ensure_folders` records and
commits the folders Drive did create before it re-raises, and every check or background scan of an open project first
creates whatever declared folder is still missing (`actions._repair_folders`), so a Drive outage heals by itself.

Titlebars is a single folder, `Final Render/Titlebars`, without format folders. For one evening it was split into
`ProRes 4444` and `Hap/Hap Alpha`; projects that declared it then have records of those two folders
(`constants.OBSOLETE_FOLDER_KEYS`). The same repair step makes the bot forget them
(`projects.forget_obsolete_folders`): the two records are dropped, and **nothing is changed on Google Drive**. A
file only exists on Drive once its upload has finished, so a folder that lists as empty may be receiving one; whether
such a folder can go is for a person to decide. Files inside them count for `Titlebars`, as deep as the check looks
(three levels below the Titlebars folder, so up to one sub-folder inside a former format folder). Archived and
cancelled projects keep their records until they are open again.

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
ACTIVE / INCOMPLETE / READY_FOR_VERIFICATION ──(Team Lead "Revoke", confirmed)──▶ CANCELLED   (Drive folder trashed first; preview rows removed; group notice)
CANCELLED ──(Team Lead "Restore")──▶ ACTIVE   (folder untrashed)
```
CANCELLED projects are never scanned, reminded about, or returned by search; they remain in `/projects` (archived & cancelled view) and in the database, but their row is deleted from the index sheet (rebuilds also omit them; restore re-adds the row).
ARCHIVED projects are not rescanned automatically. Verify is only offered when the latest scan is complete (a fresh scan is run first).

Scans: every `SCAN_INTERVAL_MINUTES` (default 30) for all ACTIVE/INCOMPLETE/READY projects; on demand from the project menu (`Check progress`) and group `/status`.

## 6. Previews

* Sources: `timeline_prores` (iff has_timeline) and `contin_prores` (iff has_contin_videos), never the `titlebars` folder (overlays); video files by MIME `video/*` or extension `.mov .mxf .mp4 .m4v .avi`.
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

## 7a. File listing

`services/listing.py` walks the project folder (depth ≤ 6, OS junk hidden) and renders a tree: bot-created folders in archive order, other folders and files alphabetically, sizes, per-folder totals, "— empty" markers, folder names linked to Drive, split into numbered messages of ≤ 3500 raw characters. Reachable from the project menu (**Files**), every search card (**Files**) and `/files` in an MG Group (open projects of that group). Listings are cached per project for `STATUS_COOLDOWN_SECONDS` so repeated taps do not re-read Drive. Not offered for cancelled projects (folder in the trash).

## 8. Roles and authorisation

| Capability | Super Admin | Team Lead | Designer |
|---|---|---|---|
| /search, preview, open archive | ✔ | ✔ | ✔ |
| **Lights** (extra role): search + Preview only — no Open Archive / Details / Files (buttons hidden and callbacks refused), no group `/files`, never assignable | — | — | preview-only |
| /newproject, /projects, project menu (validate, announce, remind, assign, metadata, previews, verify, reopen) | ✔ | ✔ (mutating actions only on projects they lead; read-only elsewhere) | ✖ |
| /creategroup (provisioning token) | ✔ | ✔ | ✖ |
| /setpassword, /users, /groups, revoke/restore user, set role, revoke group | ✔ | ✖ | ✖ |

* Super Admin = `SUPER_ADMIN_TELEGRAM_ID` from `.env`, auto-registered, cannot be revoked/demoted.
* Every private-chat handler requires an ACTIVE user (except /start and the password step).
* Every group handler requires **both** an ACTIVE user **and** an ACTIVE MG group (chat_id in `mg_groups`). Unauthorised groups get one short denial per command; revoked groups are left.
* Group chat allows only: `/status`, `/remind` (Team Lead), `/activate <token>`, `/help`. Search/preview/project/user management commands in groups reply "private chat only" once.
* The group gate exempts `/activate` (it still requires an ACTIVE Team Lead / Super Admin) and the `my_chat_member` / migration service updates.
* Basic group → supergroup migration (chat id changes) is handled twice: a `StatusUpdate.MIGRATE` handler re-keys `mg_groups.chat_id` and `projects.mg_group_chat_id`, and every group send catches `ChatMigrated`, re-keys, and retries.
* A group revoked by the Super Admin cannot be re-activated with a Team Lead token; the Super Admin restores it from `/groups`. A group the bot was merely removed from can be re-activated with a fresh token.
* Group titles are kept current three ways: the `new_chat_title` service message (a `StatusUpdate.NEW_CHAT_TITLE` handler), the chat title attached to every group command (synced in the access gate), and a 6-hourly `get_chat` refresh that covers renames made while the bot was offline.

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
2. Asset declaration: inline toggles Timeline / Contin Videos / Contin Lyrics / Titlebars / PSD, then "Continue".
3. MG group: always an explicit choice among the ACTIVE groups (or "none — announce later"), with a hint on how to authorise a missing group. Nothing is auto-linked, so a Team Lead running several projects with separate chats always sees where the announcement will go.
4. Optional metadata (event, collection, ministry, style, colours, tags, description) via "Add metadata now" / "Skip". Year defaults to current year; creator defaults to creator's name; asset types derived.
5. Assign designers: multi-select of ACTIVE users (category ALL); refine per-category later from project menu.
6. Confirm → folders created on Drive (in a worker thread) → project ACTIVE → announcement posted to the MG group (folder links, required assets, assigned designers) → Team Lead gets the link summary.

## 11a. Moving a project between collections (project menu → Collection)

* Callbacks `pj:<id>:col` (choices) and `pj:<id>:setcol:<none|new|collection id>`; both are management actions (the project's lead or the Super Admin). `new` opens the text prompt `move_collection`; the name that is sent re-uses the collection of that name (case-insensitive) or stands for a new one.
* **A new collection is stored only after the move has happened.** Until then it is an object in memory: nobody else sees it in a menu, no row has to be taken back when the move does not happen, and collection ids are never re-used. If Google Drive fails after the collection's folder was created for this move, that folder is put in the trash again (only if this move created it and it is empty; a folder that was found is never touched).
* The button press is acknowledged before the move starts (waiting for Google Drive can take longer than Telegram allows for an answer) and the message shows “Moving…” without buttons; the outcome is shown by editing the message: the project menu after a success (also when a second press finds the project already there), the reason and the choices again after a refusal.
* `projects.move_project(project, target)` refuses drafts and cancelled projects, the place the project is already in, and a destination that already holds a project of the same name. Archived projects can be moved.
* Order: the destination collection's Drive folder is checked or created; then the project's Drive root is moved with **one** `files.update` (`addParents` / `removeParents`, the actual current parents are read from Drive first, `supportsAllDrives`); only then the database changes (`collection_id`, the `collection` label, the recorded root folder name, and the new collection's row). When Drive does not do the move, the bot's records are unchanged. If the request fails but the folder turns out to be in the destination (the answer was lost), the move counts as done. Folder ids never change, so stored links, previews and the sub-folder records stay valid. A folder that somebody moved by hand in Google Drive is handled the same way: moving the project there with the bot only updates the records.
* The collection folder (`collections.ensure_collection_folder`, also used by the wizard): a recorded folder is looked up on Drive before it is used. If it is in the trash or gone, it is replaced by a new one when no project lives in the collection, and the request is refused when projects still do (their folders are inside it). Cancelled projects count for as long as their own folder still exists in the trash, because they can be restored. A folder of the collection's name under the archive root is re-used **unless it is a project's folder**: a collection is never given a project's folder.
* Names: a new collection cannot be given the name of a project that sits at the top level (compared without case; refused when the name is sent, in the wizard and in the move), with one exception: the project being moved may have that name, and then becomes the first member of a collection of its own name. The reverse is allowed: a top-level project may be created or renamed to the name of an existing collection (its Drive folder gets a ` (2)` suffix).
* Earlier versions re-used a top-level project's folder for a collection of the same name. Such a collection keeps working while projects live in it (they are inside that folder already); only the project that owns the folder cannot be moved into it. Once it is empty it gets a folder of its own the next time it is used.
* If the destination already contains a folder of that name that is not this project, the Drive folder gets a ` (2)` suffix in the same request; the project name itself never changes. It gets its plain name back when it later moves somewhere the name is free.
* A free-text `collection` label of a top-level project is replaced by the collection's name (the Collection menu says so beforehand) and is empty after the project is taken out again.
* Afterwards: the index sheet row is synced (a cached file listing is never served under another display name than the one it was made for, so it follows a move or rename at once), the live status message is refreshed (the display name `Collection / Project` changed) and the group is told.
* Collections are never deleted: an emptied collection and its folder remain and stay selectable. The menu shows at most 60 collections and says so when there are more; the others are reached by sending their name under “New collection…”.

## 12. Group messages

* **Announcement** (on creation / "Announce"): project name, Drive root link, per-folder links for required leaves, assigned designers (mentions).
* **Progress / reminder** (`/status`, daily reminder job at `REMINDER_HOUR` local time, "Remind" button): ✅/❌ per required leaf, missing items with responsible designers mentioned. Reminder job skips projects reminded within the last 20 h and projects that are complete/archived.
* **Live status**: one progress message per project (`projects.status_message_id`, content hash in `status_message_hash`). Scans, private checks and state changes (verify/reopen/restore/revoke) edit it in place, skipping the API call when the text is unchanged; background work never creates it. `/status` re-posts it at the bottom (old copy deleted, or edited to "outdated" when Telegram's 48-hour delete limit applies). Edits that fail with a BadRequest (message deleted by an admin) clear the id so the next `/status` re-creates it; relinking the project to another group clears it too.
* **Ready for verification**: posted once when a scan flips the project to READY.
* **Archived**: posted when the Team Lead verifies.

## 12b. Project index sheet

* `services/sheets.py` wraps the Sheets v4 API (values get/update/append/clear, batchUpdate) with an in-memory fake for tests / fake mode. `services/tracking.py` owns the layout: 27 columns (ID … Description, `A`–`AA`; no range beyond the tab's grid is ever read or written, see the layout changes below), header frozen + bold + basic filter.
* The spreadsheet is created once in the archive root via the Drive API (`mimeType=spreadsheet`) and its id is stored in `settings` (`tracking_sheet_id`); `TRACKING_SHEET_ID` can point at an existing sheet instead.
* Sync = upsert by project ID (column A): after create, metadata/declaration/assignment/group changes, verify/reopen and any status transition from a scan. Syncs run as fire-and-forget tasks under one asyncio lock so handlers stay fast; failures are logged and DM'd to the Super Admin once. A nightly job (03:30 local) rebuilds the whole sheet from the database; `/sheet rebuild` does the same on demand.
* Layout changes (a new column in a new version): the header row is the marker of the layout. `upsert_row` and `delete_row` change nothing when the header is not `HEADERS` and return `needs_rebuild`; `rebuild` then brings the sheet up to date. What it does first depends on the header it finds (`tracking.layout_of`):
  * *current*: nothing. The filter and column widths are the user's to change, so the nightly rebuild leaves them alone.
  * *previous version* (`PREVIOUS_HEADERS` = `HEADERS` without `ADDED_COLUMNS`): the new columns are inserted where they belong with `insertDimension`. Google moves the rest of the sheet with them: the data, a column somebody added to the right of the table, saved filter views, formats and references from other tabs. The insert is sent once, without retries, because repeating an insert that did arrive would add the column twice.
  * *pending* (the inserted columns are there but still empty): an earlier attempt stopped before writing; nothing more to prepare.
  * *anything else* (empty or unknown header): the grid is widened to 27 columns with `appendDimension` (a new spreadsheet is 26 wide and Google refuses ranges beyond the grid), the frozen bold header row and the filter are set up again, and the widths are fitted after the write.

  Then the header, every row and the blanks that wipe left-over rows go out in **one** `values.update`. The table is therefore never empty and never half written: it is the old one until that request succeeds, and a rebuild that fails before it is repeated by the next sync. Rows are never left in one layout under the header of another.
* The bot only writes values in columns `A`–`AA`. Rows, however, are handled as whole sheet rows: a revoke deletes the project's row (`deleteDimension`), new rows are inserted (`INSERT_ROWS`) and a rebuild writes `A`–`AA` in project-id order. Cells that somebody keeps to the right of `AA` are therefore removed with a revoked project's row and do not stay beside the same project after a rebuild: the sheet is not the place for per-project notes.
* The database stays the source of truth; the sheet is a read-only view for humans. Scale: one row per project, well inside the 10-million-cell limit.

## 12a. Startup checks

* Password seeded once from `INITIAL_ACCESS_PASSWORD`; any DB row holding SUPER_ADMIN that is not `SUPER_ADMIN_TELEGRAM_ID` is demoted.
* The Drive root folder is fetched: unreachable → exit with a sharing hint; service-account + My Drive logs a loud ownership/quota warning. Listings use `corpora=allDrives`, so the identity may be a Shared Drive member *or* merely have the root folder shared with it. Deletions are "move to trash" because Content managers cannot permanently delete on Shared Drives.
* Leftovers in `WORK_DIR` are purged; PENDING previews are re-planned by the next scan.
* The bot never changes Drive permissions: access to archives comes from Shared Drive membership or from sharing the root folder with the team's Google accounts.
* Login lockouts are reported to the Super Admin by DM.
* Callback data is validated with `parse_int` / `parse_enum` before use (never raises on crafted data); search callbacks refuse DRAFT and CANCELLED projects; free-text inputs are bounded (`normalise_terms` caps, tag caps, `clip_message` on every builder); `/activate` is rate-limited; the error handler never echoes exception text.
* Password policy: ≥ 12 characters, placeholder deny-list, scrypt N=2¹⁴/r=8/p=5; startup refuses placeholder passwords and example config values. Process runs with `umask 077`; DB/WAL/log/work files are private. Third-party loggers are clamped so DEBUG never records update contents. Google API errors are reduced to status + fixed reason code before reaching users.
* Resource limits: listings bounded (folders/files/messages), per-user and per-chat budgets, single-flight per project on a dedicated executor; `/status` re-posts once per cooldown; previews have a source-size window, a per-scan cap and throttled failure DMs; `AIORateLimiter` queues outgoing bursts. DMs only reach active users; revocation/demotion drops assignments. Full write-up: `docs/SECURITY_REVIEW.md`. A global breaker (30 wrong passwords / 10 min across all accounts) pauses registration for 15 min and alerts once. Unknown senders are answered at most once per minute; group `/status` re-uses a check younger than 60 s instead of hitting Drive again.

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

* **Placement lock** (`access.placement_lock`): one archive-wide asyncio lock held by everything that decides a project's name or place (creating the archive in the wizard, rename, move), from the check "is this name free there?" to the commit. It is always taken inside the per-project lock. It keeps names unique when two leads act at the same moment and makes sure a collection folder is created once. Drafts do not reserve their name, so `provision_folders` checks it again when the archive is created. A rename or move that has to wait says so (“Another change to the archive is in progress…”). When its text step was closed meanwhile (`/cancel`, a command, another button, or the same step opened again: each step is its own prompt object) it is not carried out once its turn comes, and the user is told which request was dropped. A finished step only closes its own prompt, never one the user opened since.
* Names are compared without case in SQL (`lower(name)`) and in Python (`str.lower()`). SQLite's built-in `lower()` only knows A–Z, so `db.make_engine` registers a Unicode-aware `lower` on every connection; both sides then agree for every alphabet.

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
