"""The command menus Telegram shows, scoped so each person sees only what their role allows.

Telegram knows nothing about this application's roles. It stores a command list per *scope* and
serves the most specific scope that matches a chat. A private chat's id is the user's id, so
pushing a list to that chat's scope is how a role becomes a menu.

The lists are pushed at three moments: when somebody registers, when the Super Admin changes their
role, and for every active user at startup, which is what applies a change made while the bot was
stopped. Nothing here grants or withholds access: authorisation is enforced by the ``require``
decorator on every handler. This only stops the menu advertising commands the person cannot use.
"""
from __future__ import annotations

import logging

from telegram import Bot, BotCommand, BotCommandScopeChat
from telegram.error import TelegramError

from ..constants import Role
from ..db import session_scope
from ..services import users as user_service

log = logging.getLogger(__name__)

_START = BotCommand("start", "Register / show the menu")
_SEARCH = BotCommand("search", "Search the archive (comma-separated terms)")
_TAIL = [
    BotCommand("whoami", "Show your role"),
    BotCommand("cancel", "Abort the current step"),
    BotCommand("help", "Show help"),
]
_TEAM_LEAD_EXTRA = [
    BotCommand("newproject", "Create a new archive"),
    BotCommand("projects", "Manage archives"),
    BotCommand("creategroup", "Token to authorise an MG Group"),
    BotCommand("sheet", "Link to the project index sheet"),
]
_SUPER_ADMIN_EXTRA = [
    BotCommand("users", "Manage users"),
    BotCommand("groups", "Manage MG Groups"),
    BotCommand("setpassword", "Change the access password"),
]

# Designers and Lights share a private menu. The Lights restriction is on what a search result
# offers and on /files in a group, not on which commands exist in a private chat.
VIEWER_COMMANDS = [_START, _SEARCH, *_TAIL]
TEAM_LEAD_COMMANDS = [_START, _SEARCH, *_TEAM_LEAD_EXTRA, *_TAIL]
SUPER_ADMIN_COMMANDS = [_START, _SEARCH, *_TEAM_LEAD_EXTRA, *_SUPER_ADMIN_EXTRA, *_TAIL]

COMMANDS_BY_ROLE: dict[Role, list[BotCommand]] = {
    Role.LIGHTS: VIEWER_COMMANDS,
    Role.DESIGNER: VIEWER_COMMANDS,
    Role.TEAM_LEAD: TEAM_LEAD_COMMANDS,
    Role.SUPER_ADMIN: SUPER_ADMIN_COMMANDS,
}

# Served wherever no per-user scope applies: someone who has not registered yet, and anyone whose
# per-chat push has not landed. The viewer list is the right default because it advertises nothing
# a newly registered Designer cannot do, and it still offers /start to a stranger.
PRIVATE_COMMANDS = VIEWER_COMMANDS

GROUP_COMMANDS = [
    BotCommand("status", "Archive progress for this group"),
    BotCommand("files", "List the files uploaded for this group's archives"),
    BotCommand("remind", "Post reminders for missing uploads (Team Lead)"),
    BotCommand("activate", "Authorise this group with a token (Team Lead)"),
    BotCommand("help", "Show group commands"),
]


def commands_for(role: Role) -> list[BotCommand]:
    """The private-chat menu for *role*, falling back to the viewer menu for an unknown value."""
    return COMMANDS_BY_ROLE.get(role, VIEWER_COMMANDS)


async def apply_user_commands(bot: Bot, telegram_id: int, role: Role) -> bool:
    """Push *role*'s menu to that person's private chat. Returns whether it landed; never raises.

    A failure here is cosmetic, so it is logged and swallowed: the menu stays as it was and the next
    startup re-pushes it.
    """
    try:
        await bot.set_my_commands(commands_for(role), scope=BotCommandScopeChat(chat_id=telegram_id))
    except TelegramError as exc:
        log.info("Could not set the command menu for %s: %s", telegram_id, exc)
        return False
    return True


async def refresh_command_menus(bot: Bot, super_admin_id: int) -> int:
    """Re-push every active user's menu at startup. Returns how many landed.

    This is what picks up a role change made while the bot was stopped. The Super Admin is included
    even without a database row, because that account is registered from configuration on first
    contact and may not exist yet on a first run.
    """
    with session_scope() as session:
        menus = {u.telegram_id: u.role for u in user_service.list_active_users(session)}
    menus[super_admin_id] = Role.SUPER_ADMIN
    applied = 0
    for telegram_id, role in menus.items():
        if await apply_user_commands(bot, telegram_id, role):
            applied += 1
    return applied
