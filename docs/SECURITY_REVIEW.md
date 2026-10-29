# Security review — MG Archive bot

Date: 26 September 2026 · Scope: the whole bot as deployed from this repository (Telegram handlers, Google Drive/Sheets
integration, preview pipeline, configuration, deployment) · Method: OWASP-style assessment of a chat bot, combining
automated scanning, a structured manual review with adversarial verification, and hands-on abuse testing that now runs
as part of the test suite.

## 1. Threat model

| Actor | What they can do | What they must not get |
|---|---|---|
| Unregistered stranger | Message the bot, press `/start`, join a group the bot is in | Registration without the password; any data; a way to make the bot flood chats |
| Designer | Search, previews, archive links, details, file listings; upload to Drive | Team Lead actions (create / manage / verify / revoke projects), user or group management |
| Lights | Search and play previews | Drive folder links, file names, project details, anything above |
| Team Lead | Everything project-related, group provisioning | User management, password change, group revocation |
| Group member of an authorised chat | `/status`, `/files`, `/remind` (Team Lead) if also a registered user | Anything in an unauthorised group; anything as an unregistered user |
| Someone with Drive access | Read/write the shared folder as Drive permissions allow | Not a bot concern; the bot never changes Drive permissions |
| Someone with the `.env` / `secrets/` | Full control | Out of scope: protect the host |

Assets: the bot token, the service-account key, the access password (hash), users' Telegram IDs and names, project
metadata, Drive links and file names, the index sheet.

Attack surface: the bot has **no inbound network exposure** (long polling only, no port, no webhook). Every input arrives
through Telegram: commands, free text, button presses (`callback_data`, attacker-controlled), chat/user metadata
(names, titles), and Google Drive content (file names) uploaded by designers.

## 2. Automated scanning

| Tool | Result |
|---|---|
| `pip-audit` (dependency CVEs) | No known vulnerabilities in any installed package |
| `bandit -r mg_archive_bot` (SAST) | No high/medium issues after fixes. Remaining low-confidence hits are false positives: constants whose names contain "password" (`PASSWORD_KEY`, token alphabet), and `subprocess.Popen` used with an argument list and no shell |
| `ruff --select S` (flake8-bandit) | Clean apart from the same false positives |

Fixed from scanning: SHA-1 used for change-detection of the live status message replaced by SHA-256 (no security
impact, but removes a flagged primitive); a bare `except: pass` replaced by a logged `BadRequest` handler.

## 3. Hands-on abuse testing (now `tests/test_security.py`)

Each case is a permanent regression test run by `python -m pytest`.

| Case | Before | After |
|---|---|---|
| Lower roles (stranger, revoked, Lights, Designer) pressing every privileged button and command, including hand-crafted `callback_data` | Refused | Refused; verified that no database state changes and nothing is posted to groups |
| Team Lead reaching Super Admin actions; Super Admin demotion; minting a second Super Admin | Refused | Refused |
| Malformed `callback_data` (`pj:1:asg:BOGUS:1`, `ad:u:abc`, 5000-digit ids, missing parts) | **Raised exceptions** (`ValueError`, `IndexError`) → generic error to the user, stack traces in the log, and Python's integer-digit limit could be triggered | Parsed defensively (`parse_int`, `parse_enum`); invalid data answers "Invalid request" or is ignored; no exception paths |
| Draft or cancelled projects reached through search buttons (`sr:<id>:details/files/prev`) | **Cancelled projects' details and file listing were reachable**; drafts of other Team Leads too | Refused ("Project not found") for DRAFT and CANCELLED |
| Oversized inputs: 10 000-character search, 5 000 terms, 2 000 tags, 10 000-character metadata, 60 assignees | **Messages of 10–17 kB** → Telegram rejects them → search and project menus broken for everyone by one user | Terms capped (50 × 100 chars), tags capped (20 × 40 chars), mentions capped (15), description display capped (600), every builder clipped at 4 000 chars on a line boundary; echoed query truncated |
| HTML/entity injection via names, usernames, group titles, tags, metadata, Drive file names | Escaped | Escaped everywhere (verified across search, details, announcements, progress, user cards, group notices); project names may not contain `<` or `>` |
| Group commands from an authorised user in an unauthorised group, and from unregistered/revoked users in an authorised group | Refused | Refused; unknown senders answered at most once per minute (no reply amplification) |
| `/activate` token guessing | Unlimited replies | 5 attempts per user per 10 minutes, then silence (token space is 32⁸ ≈ 10¹²) |
| Password brute force | Per-account lockout + global breaker | Verified: lockout after 5, Super Admin alerted, breaker pauses registration, password never echoed, password messages deleted |
| Error handler leaking internals | `BadRequest` text was meant to be echoed (dead code) | Users only ever see "Something went wrong"; details stay in the log |
| Files the bot creates | Database and log world-readable (0644) | Database, log 0600; work directory 0700; `.env` and `secrets/` 0600/0700 |
| Container | Ran as root | Runs as unprivileged user `mgbot` (uid 10001) |

## 4. Manual review (8 lenses, each finding verified by two independent skeptics)

Lenses: authorization, authentication, injection, secrets & logging, denial of service, privacy, concurrency,
configuration/supply chain. 74 agents; 33 raw findings; 30 confirmed (3 refuted as already fixed or not exploitable);
after verification none was rated above **medium**. Duplicates across lenses merged below. Every item is fixed unless
marked otherwise, and every fix has a regression test in `tests/test_security.py`.

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | medium | File listings had unbounded Drive cost and output (one `/files` on a folder with a PNG sequence sent dozens of messages; concurrent presses each walked Drive; walks ran on the thread pool shared with scans and previews) | Walk bounded to 200 folders / 2 000 files with an explicit notice; at most 4 messages per listing with a Drive link for the rest; names cut to 120 characters; 2 listings per user per minute and 1 per group per minute; single-flight per project; listings run on their own 2-thread pool |
| 2 | medium | The first-run password accepted the public placeholder from `.env.example` and only 8 characters | 12-character minimum, placeholder/common-password deny-list and repetition check for both first run and `/setpassword`; the bot refuses to start while a placeholder password is stored or example config values remain; scrypt cost raised to OWASP's N=2¹⁴, r=8, p=5 |
| 3 | medium | `LOG_LEVEL=DEBUG` would make the Telegram library log every incoming update, including passwords typed at registration | Third-party loggers are clamped to INFO/WARNING regardless of `LOG_LEVEL` |
| 4 | medium | OAuth mode wrote the refresh token (full-Drive scope of that account) world-readable; rotated logs and SQLite WAL/SHM files were world-readable | `umask 077` at startup and in the tools, token written 0600, data directory 0700, WAL/SHM restricted; startup warns when `.env` or key files are readable by others; README tells OAuth users to use a dedicated Google account |
| 5 | medium | `/status` re-posted the live message on every call with no throttle (message churn, orphaned duplicates under concurrency) | One scan *and* one re-post per group per cooldown; inside the window the bot points at the existing message, at most 3 times per user per 10 minutes |
| 6 | medium→low | A Designer could make the preview worker download hundreds of tiny fake `.mov` files and DM the Team Lead a failure per file; no upper bound on source size | Sources below 1 MB or above 20 GB are skipped, at most 20 new previews per project per scan, unattended failure DMs at most one per project per hour with a generic reason (details in the log) |
| 7 | medium→low | Revoked users kept receiving project DMs and stayed assigned/mentioned in group posts | DMs only go to registered *active* users; revocation (and demotion to Lights) removes assignments; assignees are filtered to active contributors; revoked users cannot be re-assigned |
| 8 | medium→low | A SQLite write transaction was held open across Drive I/O while provisioning a project inside a new collection | The collection folder is committed before the tree is built |
| 9 | low | Reply throttle for unknown senders was bypassed by `/help`, unknown commands and the revoked-user branches | All replies to unregistered or revoked senders go through the same once-per-minute throttle; stray text from them is deleted (it may be a password typed after the prompt expired) |
| 10 | low | Raw Google API errors (URLs, file ids, error bodies) were forwarded to chats | Users see only the HTTP status, a fixed hint and Google's fixed reason code; the full error stays in the log |
| 11 | low | Whoever ran `/status` became the "requester" of queued previews and received file names and errors by DM (including Lights) | Group `/status` never assigns a requester |
| 12 | low | Declared-assets toggle ran folder creation outside the per-project lock (double tap could create duplicate folders) | Runs under the project lock with a refreshed row |
| 13 | low | Dependencies partially pinned, no hash locking, base image tag floated | `requirements.lock` with hashes (installed with `--require-hashes`), base image pinned by digest, `pip-audit` clean |
| 14 | low | Container ran with default capabilities and a writable code directory; ffmpeg decodes designer-supplied media | Non-root user owning only `data/` and `secrets/`; compose drops all capabilities, `no-new-privileges`, read-only root with tmpfs `/tmp`, PID and memory limits; Telegram flood control via the library's rate limiter |
| 15 | low — accepted | Six throwaway Telegram accounts can keep the global registration breaker tripped (new members cannot register while it lasts; existing users unaffected; Super Admin is alerted each time) | Accepted: the breaker is the defence against distributed password guessing. `PASSWORD_BREAKER_FAILURES=0` disables it temporarily if that ever happens; a Super-Admin-issued invite path is the long-term option |

Refuted or already fixed before the review completed: crafted `sr:<id>` buttons reaching draft/cancelled projects
(fixed in §3), error handler echoing exception text (fixed in §3), sheet syncs per click (bounded by design).

## 5. Residual risks and operating guidance

* **Drive access is the real boundary for previews and archives.** The bot hands out links; Google decides who can
  open them. Keep the archive folder shared only with the team, and remember that anyone with Drive access can read
  everything regardless of their bot role.
* **The shared access password** is a bootstrap secret. Rotate it (`/setpassword`) after onboarding and whenever
  someone leaves; revoke departed users in `/users`. Lockout and breaker alerts arrive as DMs to the Super Admin.
* **The host is the trust boundary.** Whoever can read `.env` or `secrets/` controls the bot and its Drive access. Keep
  the machine patched, keep those files private (they are 0600), and never commit them (git-ignored).
* **Group membership is not authentication.** A group being authorised only allows the bot to post there; every
  command still requires a registered, active user.
* **Rate limits are per process and in memory.** Restarting the bot resets them; they protect against nuisance, not
  a determined distributed attacker, which Telegram itself throttles.
* If the token, key or password ever leaks: regenerate the token in @BotFather, delete the key in the Cloud Console and
  download a new one, run `/setpassword`, and review `/users`.
