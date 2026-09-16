# Motion Graphics Archive Management System

## 1. Project Context

The Motion Graphics ministry produces and maintains a large number of digital creative assets for ministry events, services, and productions.

These assets include:

- Editable working files
- Final rendered videos
- Playback formats
- Supporting resources such as fonts and design files

Currently, all projects are archived manually through Google Drive. While the existing folder structure provides organisation, the overall archive workflow requires significant manual coordination from team leads.

As the archive grows, the ministry requires an automated system to:

- Reduce administrative workload
- Improve archive consistency
- Enable fast asset discovery
- Improve long-term asset management
- Maintain secure access control

The proposed solution is a **Telegram-based Motion Graphics Archive Management System** integrated with Google Drive.

---

# 2. Current Archiving Workflow

The current workflow:

1. Motion designers prepare working files and package them.

2. Final videos are exported:
   - ProRes 4444 for archival/master quality
   - Hap/Hap Alpha for playback purposes

3. Team lead manually creates Google Drive folders.

4. Team lead manually copies Drive links into a formatted message.

5. Archive request is sent into the Motion Graphics group chat.

6. Designers upload assigned assets.

7. Team lead manually checks archive progress.

8. Team lead follows up with incomplete submissions.

9. Team lead manually updates Google Sheets tracking.

---

# 3. Current Problems

## 3.1 Manual Archive Setup

Every project requires manual:

- Folder creation
- Link generation
- Message formatting
- Tracking updates

This creates unnecessary repetitive work.

---

## 3.2 Manual Progress Tracking

Team leads must manually determine:

- Which assets are uploaded
- Which designers are incomplete
- Which files are missing

There is no automated archive validation.

---

## 3.3 Poor Asset Discoverability

Although assets are stored in Google Drive, finding old projects is difficult.

Users often rely on memory of:

- Project names
- Events
- Visual style
- Colours
- Keywords
- Production context

There is no dedicated asset discovery system.

---

## 3.4 No Quick Preview System

Currently users need to:

1. Open Google Drive
2. Navigate folders
3. Find video files
4. Download/open them

A better system should allow users to:

- Search assets
- Preview videos immediately
- Open full archives only when required

---

## 3.5 Group Chat Noise

The current archive workflow happens inside group chats.

This creates unnecessary messages:

- Archive setup
- Links
- Progress updates
- Reminders

The system should separate:

- Private archive management
- Group archive notifications

---

# 4. Proposed Solution

Develop a Telegram bot that functions as an internal Motion Graphics Digital Asset Management (DAM) system.

Architecture:

```
Telegram Bot
      |
      |
Application Backend
      |
 ┌────┴────┐
 |         |
Google   Database
Drive    Metadata
```

## Components

### Telegram Bot

Handles:

- User interaction
- Archive management
- Asset discovery
- Notifications
- Permissions

### Google Drive

Stores:

- Working files
- Master renders
- Playback files
- Preview files

### Database

Stores:

- Users
- Roles
- Projects
- Tags
- Metadata
- Archive status
- Permissions

---

# 5. Bot Modes

The bot has two different behaviours.

---

# Private Chat Mode

Private chat is the main interface.

Used for:

- Archive creation
- Asset discovery
- Search
- Preview
- Project management
- Administration

---

# MG Group Mode

The bot acts only as an archive manager.

Used for:

- Archive announcements
- Progress tracking
- Reminders
- Completion updates

The following are NOT allowed inside groups:

- Archive creation
- Asset search
- Preview discovery
- User management

This prevents group chat spam.

---

# 6. Google Drive Archive Structure

Every project follows a fixed structure.

```
Project Name/

├── Working File/
│   ├── Fonts/
│   ├── AE/
│   └── PSD/ (optional)
│
└── Final Render/
    ├── Timeline/
    │   ├── ProRes 4444/
    │   └── Hap/Hap Alpha/
    │
    ├── Contin Videos/
    │   ├── ProRes 4444/
    │   └── Hap/Hap Alpha/
    │
    └── Contin Lyrics/
        └── PNG/
```

## Folder Rules

Always create:

- Fonts
- AE
- Timeline
- Timeline/ProRes 4444
- Timeline/Hap/Hap Alpha
- Contin Videos
- Contin Videos/ProRes 4444
- Contin Videos/Hap/Hap Alpha
- Contin Lyrics
- Contin Lyrics/PNG

Only optional folder:

```
Working File/PSD
```

---

# 7. Asset Requirement Declaration

Folder existence does not determine whether an asset is required.

During project creation, the Team Lead declares:

```
Timeline Assets:
Yes / No

Contin Videos:
Yes / No

Contin Lyrics:
Yes / No

PSD:
Yes / No
```

Example:

```
Timeline Assets: No
Contin Videos: Yes
Contin Lyrics: Yes
PSD: No
```

The bot still creates:

```
Timeline/
Contin Videos/
Contin Lyrics/
```

However:

- Empty Timeline is not considered incomplete.
- Empty Contin Videos is not considered incomplete if disabled.
- Empty Contin Lyrics is not considered incomplete if disabled.

Only declared assets are validated.

---

# 8. Archive Validation

Google Drive is the source of truth.

Do not rely on users manually checking completion boxes.

Validation checks:

## Always required

```
Working File/
├── Fonts/
└── AE/
```

## Conditional

### Timeline

Only validate if:

```
has_timeline_assets = true
```

Check:

```
Timeline/
├── ProRes 4444/
└── Hap/Hap Alpha/
```

---

### Contin Videos

Only validate if:

```
has_contin_videos = true
```

Check:

```
Contin Videos/
├── ProRes 4444/
└── Hap/Hap Alpha/
```

---

### Contin Lyrics

Only validate if:

```
has_contin_lyrics = true
```

Check:

```
Contin Lyrics/
└── PNG/
```

---

Archive states:

```
DRAFT

ACTIVE

INCOMPLETE

READY_FOR_VERIFICATION

ARCHIVED
```

Automated validation leads to:

```
READY_FOR_VERIFICATION
```

Human Team Lead verification changes:

```
READY_FOR_VERIFICATION
        |
        v
ARCHIVED
```

---

# 9. MP4 Preview System

Designers already generate:

- ProRes 4444
- Hap/Hap Alpha

The bot does NOT generate Hap.

The system generates MP4 previews from ProRes files.

---

## Preview Sources

Generate previews from:

### Timeline

```
Final Render/Timeline/ProRes 4444/
```

if:

```
has_timeline_assets = true
```

---

### Contin Videos

```
Final Render/Contin Videos/ProRes 4444/
```

if:

```
has_contin_videos = true
```

---

### Contin Lyrics

No MP4 preview required.

PNG previews may be added separately later.

---

# Preview Requirements

A project may contain:

- Zero previews
- One preview
- Multiple previews

Example:

```
Youth Camp 2026

Contin Videos:

Clouds.mp4
Particles.mp4
Cross.mp4
```

Therefore use:

```
Project
   |
   |
   +── Preview Assets
```

not:

```
Project
   |
   └── Single Preview
```

---

# 10. Asset Discovery

The Telegram bot acts as a searchable archive library.

Search happens ONLY in private chat.

Example:

```
/search worship, gold, particles
```

Interpretation:

```
worship
AND
gold
AND
particles
```

---

# Search Requirements

Search:

- Project name
- Collection
- Tags
- Description
- Event
- Ministry
- Style
- Colours
- Year
- Creator
- Asset type

Support:

- Case insensitive
- Whitespace trimming
- Duplicate removal

Rank:

1. Exact tag match
2. Project name match
3. Event/collection match
4. Metadata match
5. Description match

---

# Search Result

Example:

```
🎬 Easter Opening 2026

#worship
#gold
#particles

3 previews available

[Preview]
[Open Archive]
[Details]
```

Preview is sent privately.

Archive opens Google Drive.

---

# 11. User Roles

The system has three roles.

---

# Super Admin

The creator of the bot.

Only Super Admin can:

- Change bot password
- View authorised users
- View authorised groups
- Revoke users
- Restore users
- Change roles
- Revoke groups

Super Admin identity must be tied to a fixed Telegram User ID.

---

# Team Lead

Can:

- Create projects
- Manage archives
- Assign designers
- Configure assets
- Monitor progress
- Verify completion
- Create MG Groups
- Invite users into MG Groups
- Manage project metadata

Cannot:

- Change password
- Manage global users
- Revoke users
- Manage global groups

---

# Designer

Can only:

- Search archive
- Preview MP4
- Open archives

Cannot modify workflows.

---

# 12. Authentication

First-time users:

```
/start
```

Bot:

```
Enter access password
```

After successful verification:

```
User registered
Role = Designer
```

Users do not need to enter the password again.

Store:

- Telegram ID
- Name
- Role
- Status

Statuses:

```
ACTIVE
REVOKED
```

Revoked users cannot re-register using the password.

---

# 13. MG Group System

Feature name:

```
Create MG Group
```

Not:

```
Create Archive Group
```

Only MG Groups created through the bot are authorised.

Flow:

1. Team Lead selects:

```
Create MG Group
```

2. Bot creates provisioning token.

3. Team Lead creates Telegram group.

4. Bot is added.

5. Bot verifies token.

6. Group becomes authorised.

Unauthorised groups cannot use bot functions.

---

# 14. Security Model

Access requires:

```
Authorised User
+
Authorised MG Group
```

Examples:

Unauthorised user in authorised group:

```
DENY
```

Authorised user in unauthorised group:

```
DENY
```

---

# 15. Expected Benefits

## Reduce Administrative Work

Automate:

- Folder creation
- Link generation
- Archive tracking
- Reminders
- Status updates

---

## Improve Archive Quality

Ensure:

- Consistent structure
- Correct asset tracking
- Reduced missing files

---

## Improve Asset Reuse

Allow designers to:

- Search old projects
- Preview assets instantly
- Reuse existing motion graphics

---

## Improve Security

Ensure:

- Controlled access
- Role-based permissions
- Approved MG Groups only

---

# 16. Long-Term Vision

Transform the Motion Graphics archive from a passive storage system into an active searchable creative library.

Instead of relying on individuals remembering where assets are stored, the ministry can:

- Search
- Preview
- Retrieve
- Reuse
- Preserve

creative assets efficiently over many years.