# Before you begin

## What this bot does for you

The MG Archive Bot is an assistant that lives inside Telegram. It looks after the Motion Graphics archive
on Google Drive so that nobody has to do it by hand.

It does four things.

- **It sets up archives.** It creates the full set of Google Drive folders for a new production and posts
  the request, with links, into the Motion Graphics group.
- **It tracks progress.** It looks inside Google Drive by itself and knows which files have arrived and
  which are still missing. Nobody has to tick a checklist.
- **It reminds people.** It posts a daily reminder in the group listing what is still outstanding, and it
  names the people responsible.
- **It helps you find old work.** You can search the whole archive by keyword and watch a small preview of
  a past video in seconds, without downloading anything.

## What you need

| You need | Notes |
|---|---|
| Telegram | On your phone or computer. Either works. |
| The access password | Ask your Team Lead. You will only ever type it once. |
| Google Drive access | So that you can open the archive folders. If a folder will not open, ask your Team Lead. |
{widths:24,76}

> **You do not need to install anything.** The bot runs on its own. You only use Telegram.

## How to read this manual

You do not need to read all of it. Find your role below and read the chapters listed.

| Your role | Read these chapters |
|---|---|
| **Designer** | 2 Getting started, 3 Finding past work, 4 Uploading your files, 9 Inside the MG Group, 11 Quick reference |
| **Team Lead** | Everything except chapter 10 |
| **Super Admin** | Everything |
{widths:20,80}

Each set of instructions is written as numbered steps. Where it helps, the manual shows you what the bot
puts on your screen, printed in a grey box like this:

```
Welcome back, Sarah Lim (Designer).
```

> In Telegram these messages also contain small colour icons, for example a green tick for a folder that is
> complete and a red cross for one that is still empty. The grey boxes in this manual spell those out in
> words so that they are clear in print.

## The two places you will use the bot

This is the single most important idea in the manual. The bot behaves differently in two places.

| Where | What it is for | What works there |
|---|---|---|
| **Private chat with the bot** | Your own workspace. Nobody else sees it. | Searching, previews, creating archives, managing archives, administration |
| **The MG Group** | The shared Motion Graphics group chat. | Archive announcements, progress, reminders. Three commands only |
{widths:24,38,38}

Searching and project management are blocked inside the group on purpose. If everyone searched in the group
chat, the chat would fill with archive traffic and become unusable. If you try, the bot will politely tell
you to message it privately.

## What your role allows

There are three roles. Everyone starts as a Designer.

| You can | Designer | Team Lead | Super Admin |
|---|---|---|---|
| Search the archive, watch previews, open archives | Yes | Yes | Yes |
| Create archives and manage them | No | Yes | Yes |
| Assign designers and verify finished archives | No | Yes | Yes |
| Create a new MG Group | No | Yes | Yes |
| Open the project index sheet | No | Yes | Yes |
| Add, promote, revoke or restore users | No | No | Yes |
| Revoke or restore MG Groups | No | No | Yes |
| Change the access password | No | No | Yes |
{widths:46,18,18,18}

If you need a Team Lead role, ask the Super Admin to promote you. You do not need to register again.

# Getting started

Everybody does this once. It takes less than a minute.

## Step 1: Open the bot

1. Open Telegram.
2. Search for the bot by its name or its `@username`. Your Team Lead will give you this.
3. Open the chat and tap **Start**, or type `/start` and send it.

## Step 2: Enter the access password

The bot replies:

```
Enter access password

This bot is private to the Motion Graphics ministry.
Send the access password to register.
```

1. Type the password your Team Lead gave you.
2. Send it.

The bot deletes your password message straight away so that it does not sit in the chat history. This is
normal and it means the system is working correctly.

When the password is right, you will see:

```
User registered
Role = Designer

You won't need the password again.
```

> **You will never be asked for the password again.** The bot remembers your Telegram account. If you get a
> new phone and sign in to the same Telegram account, you stay registered.

**If you type the password wrongly**, the bot tells you how many attempts you have left. After five wrong
attempts the bot locks your account for fifteen minutes and tells the Super Admin. Wait for the fifteen
minutes to pass and try again with the correct password.

## Step 3: Check what you can do

Send `/help`. The bot lists only the commands your role allows, so this is the quickest way to see what is
available to you.

Send `/whoami` to see your name, your role and your status:

```
Sarah Lim
Role: Designer
Status: ACTIVE
```

## How to use commands and buttons

There are two ways to tell the bot what you want.

- **Commands** start with a slash, for example `/search` or `/help`. Type them and send them like a normal
  message. Telegram also shows a menu of commands when you type `/`.
- **Buttons** appear underneath the bot's messages. Tap them. Nothing is typed and nothing can go wrong.

Most of your work will be tapping buttons. Commands are only the starting point.

## If you get stuck

| Situation | What to do |
|---|---|
| The bot asked you a question and you want to stop | Send `/cancel`. The step is abandoned and nothing is saved. |
| You started something and forgot where you were | Send the command again to start it fresh. Any half finished step is dropped automatically. |
| You waited a long time and then replied | If more than thirty minutes have passed the bot will say the step expired. Start it again. |
| You are not sure what you are allowed to do | Send `/help`. |
{widths:38,62}

# Finding past work

This chapter is for everyone. Search only works in your private chat with the bot.

## The quickest way to search

**Just type what you are looking for and send it.** You do not need a command.

```
worship, gold
```

The bot replies with the number of matches and then one card for each result.

If you prefer to use a command, `/search worship, gold` does exactly the same thing. If you send `/search`
on its own, the bot asks you for the words.

## Choosing good search words

1. **Separate your words with commas.** Every word you type must match, so commas let you narrow the
   results down.
2. **Capital letters do not matter.** `Worship` and `worship` find the same thing.
3. **Extra spaces do not matter.** The bot tidies them up for you.
4. **You do not need the hash symbol** in front of a tag, although it does no harm.

The bot looks for your words in the project name, the tags, the collection, the event, the ministry, the
style, the colours, the year, the creator, the type of asset and the description.

| If you type | You will get |
|---|---|
| `worship` | Everything connected with worship |
| `worship, gold` | Only the worship projects that are also gold |
| `easter, 2026` | Easter projects from 2026 |
| `particles, loop` | Projects tagged with both particles and loop |
| `sarah` | Projects created by, or credited to, Sarah |
{widths:30,70}

> **Every word must match.** If you get no results, take one word away and try again. Searching for
> `worship` will always find more than `worship, gold, particles, cinematic`.

Results are shown five at a time, best match first. Tap **More results** at the bottom to see the next five.

## Reading a result

Each result looks like this:

```
Easter Opening 2026

#worship #gold #particles

3 previews available - Archived

[ Preview ]   [ Open Archive ]   [ Details ]
```

| Part of the card | What it tells you |
|---|---|
| The name at the top | The project name. If it belongs to a collection it reads `Collection / Project` |
| The words with a hash | The tags someone gave the project. These are the best search words |
| `3 previews available` | How many short preview videos you can watch straight away |
| `Archived` | The state of the archive. `Archived` means it is finished and verified |
{widths:30,70}

## Watching a preview

1. Tap **Preview**.
2. If there is one preview, the bot sends you a button that says **Play preview on Google Drive**.
3. If there are several, the bot lists them by file name with their size. Tap the one you want.
4. The video opens in the Google Drive player and starts playing.

> **Nothing is downloaded to your phone.** The preview is a small MP4 that streams. The original master
> file can be several gigabytes and is never opened.

If a project shows no previews, there is nothing to worry about. It simply means no video masters have been
uploaded yet, or the project never had any video to preview.

## Opening the full archive

Tap **Open Archive**. Google Drive opens at the project folder and you can browse every file in it.

If Google Drive says you do not have access, the archive folder has not been shared with your Google
account. Ask your Team Lead. The bot does not control who can open Drive folders.

## Seeing all the details

Tap **Details**. You get the full record:

```
Easter Opening 2026
Archived

#worship #gold #particles

Collection: Easter 2026
Event: Easter Service
Ministry: Worship
Style: cinematic
Colours: gold, navy
Year: 2026
Creator: Sarah Lim

A slow gold particle build for the Easter opening sequence.

Declared assets: Timeline, Contin Videos
Previews: 3 available
Archive: Google Drive
Assigned: Daniel Ng, Sarah Lim
Created by: Sarah Lim - 04 Feb 2026 14:10

Latest check
Done  Working File / Fonts (2 files)
Done  Working File / AE (1 file)
Done  Final Render / Timeline / ProRes 4444 (3 files)
Done  Final Render / Timeline / Hap/Hap Alpha (3 files)
04 Mar 2026 09:30
```

This is the fastest way to see who made something, when, and what was delivered.

# For designers: uploading your files

## How a job reaches you

When a Team Lead creates an archive, the bot posts an announcement in the MG Group. If you have been
assigned, your name is mentioned so you get a notification.

```
New archive: Easter Opening 2026

Open project folder

Required uploads
- Working File / Fonts - open                          Sarah Lim
- Working File / AE - open                             Sarah Lim
- Final Render / Timeline / ProRes 4444 - open         Daniel Ng
- Final Render / Timeline / Hap/Hap Alpha - open       Daniel Ng

Assigned: Daniel Ng, Sarah Lim

Upload your files into the folders above. I check Google Drive
automatically and will post progress here.
```

Every line in **Required uploads** is a link. Tap the link and Google Drive opens at exactly the right
folder. You never have to go looking for it.

## Where each file goes

Every archive has the same shape, so once you know it you know it for every project.

```
Easter Opening 2026/
  Working File/
    Fonts/              every font used in the project
    AE/                 the packaged After Effects project
    PSD/                Photoshop files, only when the project asks for them
  Final Render/
    Timeline/
      ProRes 4444/      the master render
      Hap/Hap Alpha/    the playback render
    Contin Videos/
      ProRes 4444/      the master renders
      Hap/Hap Alpha/    the playback renders
    Contin Lyrics/
      PNG/              the lyric image files
  _Previews/            the bot puts preview videos here. Leave it alone
```

> **Only the folders listed in the announcement are required.** Every project creates the whole tree, but a
> project that does not need Contin Lyrics is not waiting for them. If a folder is not in the announcement,
> you do not have to fill it.

## What counts as done

A folder is counted as done as soon as it holds **at least one real file**.

| This counts | This does not count |
|---|---|
| Any real file in the folder | An empty folder |
| Files inside a sub folder you made, up to three levels deep | Files of zero bytes |
| Any file type | Hidden files whose name starts with a full stop |
| | System files such as `.DS_Store`, `Thumbs.db` and `desktop.ini` |
{widths:50,50}

The last two rows matter in practice. If a folder looks full to you but the bot still says it is empty,
check that the files in it are real files and not synchronisation leftovers.

## How the bot checks

The bot looks inside Google Drive by itself, about every half hour. You do not have to tell it that you
have finished, and you must not rely on anyone ticking a box.

This means two things.

1. **Upload and forget.** Your upload will be noticed on the next check.
2. **Removing a file undoes it.** If you delete a file later and the folder becomes empty, the archive goes
   back to being incomplete and the reminder starts again.

## Checking progress yourself

Send `/status` **inside the MG Group**. The bot posts the current state of that group's open archives.

```
Easter Opening 2026 - Incomplete

Done      Working File / Fonts (3 files)
Done      Working File / AE (1 file)
Missing   Final Render / Timeline / ProRes 4444      Daniel Ng
Done      Final Render / Timeline / Hap/Hap Alpha (2 files)

1 of 4 required folders still empty.
Checked 18 Sep 2026 10:02
```

Every folder name is a link. Tap the one you are responsible for and upload.

> If you see **could not check Google Drive** next to a folder, there is a temporary problem reaching
> Google. Nothing is wrong with your upload and nothing has been marked against you. Try again later.

## Reminders

Once a day, at a fixed time, the bot posts a reminder into the MG Group for every archive that is still
incomplete. It lists only what is missing, and it mentions the people responsible.

```
Reminder: Easter Opening 2026

Still missing:
Missing   Final Render / Timeline / ProRes 4444      Daniel Ng

Project folder
```

The bot will not send more than one reminder a day for the same project, and it stops completely as soon as
everything has arrived. A Team Lead can also ask for a reminder at any time.

## When previews appear

If you upload a ProRes master into a **ProRes 4444** folder for Timeline or Contin Videos, the bot makes a
small MP4 preview of it and stores it in the `_Previews/` folder. This happens on its own, usually shortly
after the next check.

Previews make your work findable and watchable by the rest of the team for years afterwards. There is
nothing you need to do to make one.

Contin Lyrics PNG files do not get a video preview.

## Five mistakes to avoid

1. **Do not rename the standard folders.** The bot looks for them by name.
2. **Do not put your files at the top level** of the project folder. They must be inside the right folder
   for the bot to see them.
3. **Do not put anything in `_Previews/`.** The bot manages that folder and removes files it did not make.
4. **Do not upload an empty placeholder file** to make a folder look finished. Zero byte files do not count
   and the archive will still be incomplete.
5. **Do not delete files after the archive is verified** unless you mean to. Removing files makes an
   archive incomplete again.

# For Team Leads: creating an archive

## Before you start

Have three things ready.

1. The **project name**, for example `Easter Opening 2026`.
2. The **list of assets** the project will deliver: Timeline, Contin Videos, Contin Lyrics, PSD.
3. The **designers** who will work on it. They must have registered with the bot already, otherwise their
   names will not appear in the list.

The whole thing takes about a minute. You can stop at any point with `/cancel`, and nothing is created on
Google Drive until you press the final button.

## The wizard, step by step

### Step 1: Start

In your private chat with the bot, send:

```
/newproject
```

### Step 2: Choose where it lives

```
New archive

Where should it live?
A collection is a folder under the archive root that groups
sub-projects (for example BF / Opening, BF / Worship).

[ Top level (no collection) ]
[ Easter 2026 ]
[ New collection... ]
```

| Choose | When |
|---|---|
| **Top level** | A standalone production. This is the usual choice |
| An existing collection | The project belongs with others you already grouped, for example another part of `BF` |
| **New collection** | You are starting a new group of related sub-projects. Type the collection name next |
{widths:26,74}

If you choose **New collection** and a folder with that name already exists under the archive root, the bot
reuses it instead of creating a second one.

### Step 3: Name the project

```
New archive at the top level.

Send the project name (for example Easter Opening 2026).
/cancel to abort.
```

Type the name and send it.

| Rule | Detail |
|---|---|
| Length | Between 2 and 100 characters |
| Characters | The signs `<` and `>` are not allowed |
| Uniqueness | The name must be new within that collection, or within the top level |

If the name is already taken the bot tells you and asks for another one. Nothing is lost.

### Step 4: Say which assets are required

```
Easter Opening 2026

Which assets will this project include? Toggle, then Continue.
Working File (Fonts, AE) is always required.

[ off  Timeline ]
[ off  Contin Videos ]
[ off  Contin Lyrics ]
[ off  PSD ]
[ Continue ]
```

Tap each asset the project will deliver. Tapping switches it on, tapping again switches it off. When the
list is right, tap **Continue**.

> **This is the most important step in the whole manual.** Only the assets you switch on here are checked
> and chased. If you switch on Contin Lyrics for a project that has no lyrics, the archive will never be
> complete and your designers will be reminded about it every day. If you forget to switch on Timeline, a
> missing master will never be noticed.

Fonts and AE are always required, so they are not in the list.

You can change this later from the project menu if you get it wrong.

### Step 5: Choose the MG Group

```
Which MG Group should receive announcements and progress?

[ Motion Graphics Team ]
[ No group (announce later) ]
```

This step is skipped if only one group is authorised, because the bot picks it for you. If no group has
been authorised yet, the bot tells you and you can link one later.

### Step 6: Add the search information

```
Add metadata now (event, collection, style, colours, tags...)?
It makes the project searchable. You can also add it later.

[ Add metadata now ]   [ Skip ]
```

Tap **Add metadata now**. The bot then asks seven short questions, one at a time. Answer each one, or send
a single hyphen `-` to skip that question.

| Question | Example answer | Why it matters |
|---|---|---|
| Event | `Easter Service` | People search by the event they remember |
| Collection | `Easter 2026` | Groups the project with related work. Skip this one if the project already sits inside a collection folder, because the name comes from that folder |
| Ministry | `Worship` | Lets a ministry find all of its own material |
| Style | `cinematic` | People often remember how something looked |
| Colours | `gold, navy` | The single most common way people describe a past project |
| Tags | `worship, gold, particles` | The strongest search words of all. Separate them with commas |
| Description | `A slow gold particle build for the opening sequence.` | Free text, searched last |
{widths:16,28,56}

> **Tags are worth the thirty seconds.** An exact tag match always ranks above everything else, so a
> project with good tags is the one that people will find and reuse. A project with no tags is very likely
> to be forgotten.

The year is filled in for you with the current year, and the creator is filled in with your name.

### Step 7: Assign the designers

```
Who is working on this project? Toggle designers, then Done.
A star marks a Team Lead.

[ off  Daniel Ng ]
[ on   Sarah Lim ]
[ off  Joel Tan  * ]
[ Done ]
```

Tap each person who will work on the project, then tap **Done**. The people you choose here are responsible
for everything. You can narrow that down folder by folder afterwards.

Assigned people are mentioned by name in the announcement and in every reminder, so they get a Telegram
notification.

### Step 8: Check and create

```
Ready to create: Easter Opening 2026

Declared assets: Timeline, Contin Videos
MG Group: Motion Graphics Team
Assigned: Daniel Ng, Sarah Lim
Tags: worship, gold, particles

Creating the archive makes the full folder tree on Google Drive
and posts the announcement to the MG Group.

[ Create archive ]   [ Cancel ]
```

Read the summary. If it is right, tap **Create archive**.

## What happens next

Within a few seconds:

1. The full folder tree is created on Google Drive.
2. The project becomes **Active**.
3. The announcement, with a link for every required folder, is posted into the MG Group and the assigned
   designers are mentioned.
4. A row for the project appears in the project index sheet.
5. The project menu opens in your private chat so that you can carry on managing it.

## If creation fails

If Google Drive cannot be reached, the bot tells you what went wrong and offers a **Retry** button. Your
draft is kept, so you do not have to type everything again. Fix the problem, or ask your administrator to,
and tap **Retry**.

# For Team Leads: managing an archive

## Opening a project

Send `/projects` in your private chat.

```
Open projects

7 projects - page 1/1
Tap a project to manage it.

[ Easter Opening 2026 ]
[ Youth Camp 2026 ]
[ BF / Worship ]
...
[ Show archived & cancelled ]
```

Tap the project you want. To see finished or cancelled work instead, tap **Show archived & cancelled**.

If you know the number of a project, `/project 42` opens it directly.

## The project menu

Tapping a project shows its full record followed by these buttons.

| Button | What it does |
|---|---|
| **Check progress** | Looks at Google Drive right now and shows what is there and what is missing |
| **Details** | Shows the full record again |
| **Announce** | Posts the archive request into the MG Group again |
| **Remind** | Checks Drive, then posts a reminder listing only what is missing |
| **Assign designers** | Changes who is responsible, for everything or folder by folder |
| **Edit metadata** | Changes the event, ministry, style, colours, tags, description, year and creator |
| **Declared assets** | Changes which assets are required |
| **MG group** | Changes which group receives the announcements |
| **Generate previews** | Makes preview videos now, instead of waiting for the next check |
| **Previews** | Lists the previews that are ready, with a play button for each |
| **Verify & archive** | Signs the archive off as complete. Appears only when everything is present |
| **Reopen** | Reopens an archived project for more uploads |
| **Revoke project** | Cancels a project that will not go ahead and trashes its folder |
| **Restore project** | Undoes a revoke |
| **Open in Google Drive** | Opens the project folder |
| **Projects** | Goes back to the list |
{widths:26,74}

The menu changes with the state of the project, so you will not see every button at once. **Verify &
archive** only appears when the project is ready, and **Reopen** only appears once it has been archived.

## Check progress

Tap **Check progress**. The bot reads Google Drive immediately and replies with a line for every required
folder, the number of files it found, and the names of the people responsible for anything missing.

Use this before a deadline, or whenever someone tells you they have uploaded.

If the reply says Google Drive could not be checked, nothing has changed and nothing has been recorded.
Wait and try again.

## Announce and Remind

| Use | When |
|---|---|
| **Announce** | The original announcement got buried, the group is new, or you changed the assignments and want everyone to see the current list |
| **Remind** | You want to chase the outstanding items now instead of waiting for the daily reminder |
{widths:20,80}

**Remind** checks Drive first, so it never chases somebody for a file that has already arrived. If nothing
is missing the bot tells you so and posts nothing.

Both buttons need the project to be linked to an MG Group. If it is not, use **MG group** first.

## Assign designers

1. Tap **Assign designers**.
2. Choose what you are assigning for:

```
Easter Opening 2026 - assign designers
Pick what to assign for. "All assets" covers every folder.

[ All assets (2) ]
[ Working File (0) ]
[ Timeline (1) ]
[ Contin Videos (0) ]
```

3. Tap the people to switch them on or off, then tap **Done**.

Use **All assets** for people who are responsible for the whole project. Use a specific folder group when
one person is doing the After Effects work and someone else is doing the renders. The number in brackets
tells you how many people are already assigned to that group.

Reminders name the right people because of this: a person assigned only to Timeline is never chased about
Contin Lyrics.

## Edit metadata

Tap **Edit metadata** to see every field and its current value, then tap a field to change it.

| To do this | Send |
|---|---|
| Set a value | The new text |
| Clear a value | A single hyphen `-` |
| Leave it alone | `/cancel` |
{widths:30,70}

Tags replace each other: sending `worship, gold` sets exactly those two tags and removes any others. The
year must be a four digit number between 1990 and 2100.

If the project sits inside a collection folder, the Collection field cannot be edited here. It follows the
folder name.

> It is worth returning to **Edit metadata** when a project is finished and adding the tags you only thought
> of once you saw the final render. Two minutes of tagging is what makes an asset reusable.

## Declared assets

Tap **Declared assets** to switch a category on or off after creation.

- Switching one **on** makes it required, and the bot creates any folder that is missing on Google Drive.
- Switching one **off** stops it being chased. The folder and any files in it stay where they are.

Use this the moment you learn that a project no longer needs something. It immediately stops the daily
reminder for that item.

## MG group

Tap **MG group** to choose which group receives this project's announcements and progress, or to move it to
a different group. Only groups that have been authorised appear in the list.

## Previews

| Button | Use |
|---|---|
| **Generate previews** | Makes previews now. Use it after a large upload, or to retry a preview that failed |
| **Previews** | Lists the previews that are ready. Tap one to play it |
{widths:26,74}

The bot messages you privately as each preview becomes ready, or tells you why one failed. Previews are
made only from ProRes files in the Timeline and Contin Videos folders, and only for the categories the
project declared.

If you tap **Generate previews** and the bot replies that no ProRes video files were found, the masters
have not been uploaded yet.

## Verify and archive

This is the one step that a person must do. The bot can prove that files exist; only you can say that they
are the right files.

1. Open the project. When everything declared is present, the state reads **Ready for verification** and a
   **Verify & archive** button appears.
2. Tap it. The bot re-checks Google Drive first, so you are never signing off a stale result.
3. Confirm:

```
All declared assets are present for Easter Opening 2026.

Mark it as ARCHIVED? This closes the archive and announces
completion in the MG Group.

[ Yes, archive it ]   [ Cancel ]
```

4. The project becomes **Archived** and the group is told, with your name on it.

If something has gone missing since the last check, the bot refuses and shows you what is now missing
instead of archiving it.

An archived project is no longer scanned or chased, but it stays fully searchable for the future.

## Reopen

Tap **Reopen** on an archived project when something needs to be added or replaced. The project becomes
active again, checking resumes, and the group is told.

## Revoke and restore a project

Use **Revoke project** when a production is cancelled and the archive should not exist.

1. Tap **Revoke project**.
2. Read the confirmation. It tells you how many files were in the required folders at the last check, so
   you can see what you are about to trash.
3. Tap **Yes, revoke and trash the folder**.

This does five things: the Google Drive folder goes to the trash, tracking and reminders stop, the project
disappears from search, its row is removed from the index sheet, and the MG Group is told.

> **It is reversible.** Open the project from **Show archived & cancelled** and tap **Restore project**. The
> folder comes back out of the trash, the row returns to the index sheet and tracking resumes. Google keeps
> trashed items for thirty days, so do not leave a restore for longer than that.

An archived project cannot be revoked directly. Reopen it first.

# For Team Leads: the project index sheet

## What it is

The bot keeps a Google Sheet called **MG Archive Index** in the archive root folder. It holds one row for
every project ever created and it updates itself. Nobody has to maintain it.

Use it when you want to look at the archive as a list: to filter by year, to sort by ministry, to count how
many projects a designer worked on, or to hand a summary to somebody who does not use Telegram.

## Getting the link

Send `/sheet` in your private chat.

```
Project index - 74 projects, one row each, updated automatically.
https://docs.google.com/spreadsheets/d/...

Send /sheet rebuild to rewrite it from the database.
```

## What the columns mean

| Columns | What they hold |
|---|---|
| ID, Collection, Project, Status, Year | Which project it is and where it stands |
| Event, Ministry, Style, Colours, Tags, Asset types | The searchable information |
| Timeline, Contin Videos, Contin Lyrics, PSD | Yes or no for each declared asset |
| Assigned, Created by, Created, Archived, Verified by, MG Group | Who did what and when |
| Previews, Last checked, Drive link, Description | How many previews exist, when the archive was last checked, and where it is |
{widths:34,66}

The top row is frozen and filtered, so you can sort and filter without losing the headings.

## Keeping it right

The sheet updates whenever a project changes, and it is rewritten completely every night as a safety net.
If you ever think a row is wrong, send:

```
/sheet rebuild
```

The bot rewrites every row from its own records and tells you how many projects it wrote.

> Treat the sheet as a **read only report**. Editing a cell by hand will not change anything in the bot, and
> your edit will be overwritten the next time that project changes or the nightly rebuild runs.

# For Team Leads: creating an MG Group

## What an MG Group is

An MG Group is a Telegram group chat that the bot has been told to trust. The bot only posts announcements,
progress and reminders in groups that have been authorised this way. Any other group is ignored, and the
bot leaves it after an hour.

Most ministries only ever need one. Create a second one only when a separate team genuinely needs its own
notification channel.

## Step by step

1. In your **private chat** with the bot, send:

```
/creategroup
```

2. The bot replies with a token:

```
Create MG Group

Your provisioning token: MG-4K7P-R2WQ (valid 24 h, single use)

1. Create the Telegram group.
2. Add @your_bot_name to it.
3. If you added the bot yourself, the group is authorised automatically.
   Otherwise send /activate MG-4K7P-R2WQ inside the group.
```

3. In Telegram, create the group and add your team members.
4. Add the bot to the group.
5. **If you added the bot yourself, you are finished.** The bot recognises your token and confirms:

```
Motion Graphics Team is now an authorised MG Group.

I'll post archive announcements and progress here.
Use /status any time.
```

6. If somebody else added the bot, the bot will ask to be activated. Send this in the group:

```
/activate MG-4K7P-R2WQ
```

## Rules to remember

| Rule | Why |
|---|---|
| A token is valid for 24 hours | Create the group the same day |
| A token can only be used once | Ask for a new one with `/creategroup` if you need another group |
| Asking for a new token cancels your previous unused one | You only ever have one live token |
| The bot leaves a group it was added to without a token | This stops it being pulled into unrelated chats |
| A group revoked by the Super Admin cannot be reactivated with a token | Only the Super Admin can restore it |
{widths:44,56}

# Inside the MG Group

## What the bot posts there

| Post | When |
|---|---|
| Archive announcement | A new archive is created, or a Team Lead taps **Announce** |
| Progress | Somebody sends `/status` |
| Reminder | Once a day for anything incomplete, or when a Team Lead taps **Remind** |
| Ready for verification | Everything declared has arrived. Posted once |
| Archived | A Team Lead has verified the archive |
| Cancelled or restored | A project was revoked or brought back |
{widths:30,70}

## The three commands

| Command | Who can use it | What it does |
|---|---|---|
| `/status` | Everyone | Shows the progress of this group's open archives, after a fresh check |
| `/remind` | Team Lead | Checks Drive and posts a reminder for anything missing |
| `/help` | Everyone | Lists these commands |
{widths:16,20,64}

If several people send `/status` one after another, the bot reuses its last result for a minute rather than
asking Google again. This is normal and keeps things fast.

## What does not work in a group

Searching, previews, creating and managing archives, the index sheet and all administration are private
chat only. If you try one in the group, the bot replies once:

```
That only works in a private chat with me.
Open a direct message and try again.
```

This is deliberate. It is what keeps the group readable.

# For the Super Admin

Only the Super Admin account can use this chapter. It is the account whose Telegram ID is fixed in the
system configuration, and its role cannot be changed or removed from inside the bot.

## Managing people

Send `/users`.

```
Authorised users - 12 active, 1 revoked

Tap a user to manage them.

[ Sarah Lim - Team Lead ]
[ Daniel Ng - Designer ]
[ (revoked) Alex Wong - Designer ]
```

Tap a person to open their card:

```
Daniel Ng (@danielng)
Role: Designer
Status: ACTIVE
Telegram ID: 123456789
Registered: 04 Feb 2026 09:12

[ Make Team Lead ]
[ Revoke access ]
[ All users ]
```

| Button | Effect |
|---|---|
| **Make Team Lead** | The person can immediately create and manage archives. They do not register again |
| **Make Designer** | Removes Team Lead rights. Their archives are untouched |
| **Revoke access** | The person can no longer use the bot at all, and cannot register again with the password |
| **Restore access** | Gives a revoked person their access back, with their previous role |
{widths:26,74}

> **Revoking is the correct step when somebody leaves the ministry.** Changing the password alone does not
> remove anybody, because registered people are never asked for it again. Revoking is what actually closes
> the door, and a revoked person cannot come back in with the password.

Your own account is protected. You cannot revoke or demote yourself by accident, and nobody else can be
given the Super Admin role.

## Managing MG Groups

Send `/groups`.

```
MG Groups

Active   Motion Graphics Team - 1001234567890
Revoked  Old Test Group - 1009876543210

[ Revoke "Motion Graphics Team" ]
[ Restore "Old Test Group" ]
```

**Revoke** asks you to confirm, then stops all posting to that group and makes the bot leave it.
**Restore** brings a group back; if the bot had left, add it to the group again and no new token is needed.

## Changing the access password

Send `/setpassword`, then send the new password. It must be at least eight characters. Your message is
deleted immediately so the new password does not stay in the chat.

```
Access password updated.
```

Remember what this does and does not do.

| It does | It does not |
|---|---|
| Change the password that new people will need | Affect anybody who is already registered |
| Take effect immediately | Remove access from anyone |
{widths:50,50}

To remove someone's access, revoke them in `/users`.

## Alerts you will receive

The bot sends you a private message when something needs your attention.

| Alert | What it means | What to do |
|---|---|---|
| Login lockout | Somebody entered the password wrongly five times. Their name and Telegram ID are included | If you recognise them, tell them the correct password. If you do not, consider changing the password |
| Registration paused | Thirty wrong attempts across all accounts within ten minutes. Registration is paused automatically for fifteen minutes | This is the sign of a guessing attempt. Change the password with `/setpassword` |
| Project index could not be updated | The Google Sheet could not be written | The bot keeps working. Once the cause is fixed, ask a Team Lead to send `/sheet rebuild` |
{widths:24,42,34}

# Quick reference

## Private chat commands

| Command | Who | What it does |
|---|---|---|
| `/start` | Everyone | Register, or show your menu |
| *(just type words)* | Everyone | Search the archive |
| `/search worship, gold` | Everyone | Search the archive |
| `/whoami` | Everyone | Show your role and status |
| `/help` | Everyone | List the commands you can use |
| `/cancel` | Everyone | Abandon the step in progress |
| `/newproject` | Team Lead | Create a new archive |
| `/projects` | Team Lead | List and manage archives |
| `/project 42` | Team Lead | Open one archive by its number |
| `/creategroup` | Team Lead | Get a token for a new MG Group |
| `/sheet` | Team Lead | Link to the project index sheet |
| `/sheet rebuild` | Team Lead | Rewrite the index sheet |
| `/users` | Super Admin | Manage people and roles |
| `/groups` | Super Admin | Manage MG Groups |
| `/setpassword` | Super Admin | Change the access password |
{widths:26,18,56}

## Group commands

| Command | Who | What it does |
|---|---|---|
| `/status` | Everyone | Progress of this group's open archives |
| `/remind` | Team Lead | Post reminders for missing uploads |
| `/activate MG-XXXX-XXXX` | Team Lead | Authorise this group |
| `/help` | Everyone | List the group commands |
{widths:26,18,56}

## What each status means

| Status | Meaning | What happens next |
|---|---|---|
| **Active** | Open for uploads | The bot keeps checking Drive |
| **Incomplete** | Something declared is missing | Designers are reminded daily |
| **Ready for verification** | Everything declared has arrived | A Team Lead taps **Verify & archive** |
| **Archived** | Verified and closed | Nothing. It stays searchable |
| **Cancelled** | Revoked, folder in the trash | A Team Lead can restore it within thirty days |
{widths:22,34,44}

## The folder map

| Folder | What goes in it | Required |
|---|---|---|
| Working File / Fonts | Every font used | Always |
| Working File / AE | The packaged After Effects project | Always |
| Working File / PSD | Photoshop files | Only if PSD was declared |
| Final Render / Timeline / ProRes 4444 | Timeline master render | Only if Timeline was declared |
| Final Render / Timeline / Hap/Hap Alpha | Timeline playback render | Only if Timeline was declared |
| Final Render / Contin Videos / ProRes 4444 | Contin master renders | Only if Contin Videos was declared |
| Final Render / Contin Videos / Hap/Hap Alpha | Contin playback renders | Only if Contin Videos was declared |
| Final Render / Contin Lyrics / PNG | Lyric images | Only if Contin Lyrics was declared |
| `_Previews` | Preview videos made by the bot | Do not touch |
{widths:34,38,28}

# Questions and answers

**I sent the password and nothing happened.**
Check that you sent it in a private chat with the bot, not in a group. Also check for extra spaces at the
start or end.

**The bot says my account is locked.**
You entered the password wrongly five times. Wait fifteen minutes and try again with the correct password.

**I was registered and now the bot says my access was revoked.**
The Super Admin removed your access. The password will not let you back in. Speak to the Super Admin.

**I uploaded my file but the bot still says the folder is empty.**
Three things to check. Is the file in the right folder, and not one level above it? Is it a real file
rather than a zero byte placeholder or a hidden system file? And has the next check happened yet? Checks run
about every half hour, or a Team Lead can tap **Check progress** to force one now.

**The progress message says Google Drive could not be checked.**
This is temporary, usually a network problem. Nothing has been recorded against anybody and no reminder
will be sent. It corrects itself on the next successful check.

**I get reminded about a folder we do not need.**
The asset was declared when the archive was created. A Team Lead can open the project, tap **Declared
assets**, and switch it off. The reminder stops straight away.

**Search finds nothing.**
Remember that every word must match. Take a word away and try again. If a project has no tags and no
metadata, only its name can be found, which is a good reason to fill in the metadata.

**The Preview button says there are no previews.**
No ProRes masters have been uploaded yet, or the project has no video. Previews are only made from the
ProRes 4444 folders in Timeline and Contin Videos.

**A preview will not play.**
The preview opens in Google Drive, so your Google account needs access to the archive folder. Ask your
Team Lead to check the sharing.

**A command works for my colleague but not for me.**
It needs a higher role. Send `/whoami` to see your role and ask the Super Admin if you need it changed.

**The bot ignores me in the group.**
Either the group has not been authorised, or the command is private chat only. Send `/help` in the group to
see the three commands that work there.

**I revoked a project by mistake.**
Send `/projects`, tap **Show archived & cancelled**, open the project and tap **Restore project**. Do this
within thirty days, while the folder is still in the Google Drive trash.

**We archived a project and now we need to change a file.**
Open the project and tap **Reopen**. Upload the new file, then verify it again.

# !Glossary

| Word | What it means |
|---|---|
| **Archive** | All the folders and files for one production, plus its record in the bot |
| **Collection** | A folder that groups several related sub-projects, for example `BF / Opening` and `BF / Worship` |
| **Declared asset** | An asset the Team Lead said the project will deliver. Only these are checked and chased |
| **MG Group** | A Telegram group the bot has been authorised to post in |
| **Master** | The full quality ProRes 4444 render that is kept for the long term |
| **Metadata** | The information that makes a project findable: event, ministry, style, colours, tags, description |
| **Preview** | A small MP4 copy of a master that plays instantly in Google Drive |
| **Tag** | A single keyword attached to a project. The strongest thing to search by |
| **Token** | A one time code of the form `MG-XXXX-XXXX` used to authorise a new MG Group |
| **Verify** | The Team Lead's confirmation that an archive is complete and correct |
{widths:22,78}
