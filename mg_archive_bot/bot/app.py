from __future__ import annotations

import logging
from datetime import time

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeChat, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

from ..config import Settings
from ..services.drive import DriveClient
from ..util import resolve_tz
from .handlers import admin, auth, common, group_mode, mg_groups, projects, search, text_router
from .jobs import PreviewWorker, leave_stale_chats_job, reminder_job, scan_job

log = logging.getLogger(__name__)

PRIVATE_COMMANDS = [
    BotCommand("start", "Register / show the menu"),
    BotCommand("search", "Search the archive (comma-separated terms)"),
    BotCommand("projects", "Manage archives (Team Lead)"),
    BotCommand("newproject", "Create a new archive (Team Lead)"),
    BotCommand("creategroup", "Token to authorise an MG Group (Team Lead)"),
    BotCommand("whoami", "Show your role"),
    BotCommand("cancel", "Abort the current step"),
    BotCommand("help", "Show help"),
]
ADMIN_COMMANDS = PRIVATE_COMMANDS + [
    BotCommand("users", "Manage users (Super Admin)"),
    BotCommand("groups", "Manage MG Groups (Super Admin)"),
    BotCommand("setpassword", "Change the access password (Super Admin)"),
]
GROUP_COMMANDS = [
    BotCommand("status", "Archive progress for this group"),
    BotCommand("remind", "Post reminders for missing uploads (Team Lead)"),
    BotCommand("activate", "Authorise this group with a token (Team Lead)"),
    BotCommand("help", "Show group commands"),
]


async def _post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    worker = PreviewWorker(application.bot, application.bot_data)
    application.bot_data["preview_worker"] = worker
    await worker.start()
    try:
        await application.bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=settings.super_admin_telegram_id))
    except TelegramError as exc:
        log.warning("Could not register bot commands: %s", exc)
    me = await application.bot.get_me()
    log.info("Bot @%s ready (Drive mode: %s)", me.username, settings.google_auth_mode)


async def _post_shutdown(application: Application) -> None:
    worker: PreviewWorker | None = application.bot_data.get("preview_worker")
    if worker is not None:
        await worker.stop()


def build_application(settings: Settings, drive: DriveClient) -> Application:
    tz = resolve_tz(settings.timezone)
    defaults = Defaults(parse_mode=ParseMode.HTML, tzinfo=tz, link_preview_options=LinkPreviewOptions(is_disabled=True))
    builder: ApplicationBuilder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .defaults(defaults)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .concurrent_updates(True)
        .read_timeout(30)
        .write_timeout(60)
        .media_write_timeout(900)
        .connect_timeout(30)
    )
    app = builder.build()
    app.bot_data.update({"settings": settings, "drive": drive, "tz": tz})
    register_handlers(app)
    register_jobs(app, settings, tz)
    return app


def register_handlers(app: Application) -> None:
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", auth.cmd_start))
    app.add_handler(CommandHandler("help", common.cmd_help))
    app.add_handler(CommandHandler("whoami", common.cmd_whoami))
    app.add_handler(CommandHandler("cancel", common.cmd_cancel))
    app.add_handler(CommandHandler("search", search.cmd_search))
    app.add_handler(CommandHandler("projects", projects.cmd_projects))
    app.add_handler(CommandHandler("project", projects.cmd_project))
    app.add_handler(CommandHandler("newproject", projects.cmd_newproject))
    app.add_handler(CommandHandler("creategroup", mg_groups.cmd_creategroup))
    app.add_handler(CommandHandler("users", admin.cmd_users))
    app.add_handler(CommandHandler("groups", admin.cmd_groups))
    app.add_handler(CommandHandler("setpassword", admin.cmd_setpassword))
    app.add_handler(CommandHandler("status", group_mode.cmd_status))
    app.add_handler(CommandHandler("remind", group_mode.cmd_remind))
    app.add_handler(CommandHandler("activate", mg_groups.cmd_activate))

    app.add_handler(CallbackQueryHandler(projects.wizard_callback, pattern=r"^nw:"))
    app.add_handler(CallbackQueryHandler(projects.project_callback, pattern=r"^pj:\d+:"))
    app.add_handler(CallbackQueryHandler(projects.projects_list_callback, pattern=r"^pl:\d+:\w+$"))
    app.add_handler(CallbackQueryHandler(search.search_result_callback, pattern=r"^sr:\d+:\w+$"))
    app.add_handler(CallbackQueryHandler(search.search_page_callback, pattern=r"^sp:\d+$"))
    app.add_handler(CallbackQueryHandler(admin.admin_callback, pattern=r"^ad:"))

    app.add_handler(ChatMemberHandler(mg_groups.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, mg_groups.on_migrate))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, text_router.route_text))
    app.add_handler(MessageHandler(private & filters.COMMAND, common.unknown_private_command))
    app.add_error_handler(common.error_handler)


def register_jobs(app: Application, settings: Settings, tz) -> None:
    jq = app.job_queue
    if jq is None:  # pragma: no cover - job-queue extra missing
        log.error("JobQueue unavailable: install python-telegram-bot[job-queue]")
        return
    jq.run_repeating(scan_job, interval=settings.scan_interval_minutes * 60, first=120, name="scan")
    jq.run_daily(reminder_job, time=time(hour=settings.reminder_hour, minute=0, tzinfo=tz), name="reminders")
    jq.run_repeating(leave_stale_chats_job, interval=600, first=300, name="leave-stale-chats")


ALLOWED_UPDATES = [Update.MESSAGE, Update.CALLBACK_QUERY, Update.MY_CHAT_MEMBER]
