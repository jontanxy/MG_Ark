"""Minimal stand-ins for python-telegram-bot objects, exposing only what the handlers use."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

_ids = itertools.count(1)


@dataclass
class FakeUser:
    id: int
    first_name: str = "User"
    last_name: str | None = None
    username: str | None = None
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}" if self.last_name else self.first_name


@dataclass
class FakeChat:
    id: int
    type: str = "private"
    title: str | None = None


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.videos: list[dict[str, Any]] = []
        self.left: list[int] = []
        self.deleted: list[tuple[int, int]] = []
        self.commands: list[Any] = []
        self.fail_chats: set[int] = set()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        from telegram.error import Forbidden

        if chat_id in self.fail_chats:
            raise Forbidden("bot was kicked")
        msg = FakeMessage(self, FakeChat(chat_id, "private" if chat_id > 0 else "supergroup"), text=text, reply_markup=reply_markup)
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return msg

    async def send_video(self, chat_id, video, caption=None, **kwargs):
        data = video.read() if hasattr(video, "read") else video
        self.videos.append({"chat_id": chat_id, "video": data, "caption": caption, **kwargs})
        return SimpleNamespace(video=SimpleNamespace(file_id=f"file-id-{len(self.videos)}"), document=None)

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def get_me(self):
        return SimpleNamespace(username="mg_archive_test_bot", id=999)

    async def set_my_commands(self, commands, scope=None, **kwargs):
        self.commands.append((commands, scope))
        return True

    # helpers
    def texts(self, chat_id: int | None = None) -> list[str]:
        return [m["text"] for m in self.sent if chat_id is None or m["chat_id"] == chat_id]

    def last(self, chat_id: int | None = None) -> dict[str, Any]:
        msgs = [m for m in self.sent if chat_id is None or m["chat_id"] == chat_id]
        return msgs[-1]

    def last_markup(self, chat_id: int | None = None):
        return self.last(chat_id)["reply_markup"]


class FakeMessage:
    def __init__(self, bot: FakeBot, chat: FakeChat, text: str | None = None, from_user: FakeUser | None = None, reply_markup=None):
        self.bot = bot
        self.chat = chat
        self.chat_id = chat.id
        self.text = text
        self.from_user = from_user
        self.reply_markup = reply_markup
        self.message_id = next(_ids)
        self.migrate_to_chat_id: int | None = None
        self.deleted = False

    async def reply_text(self, text, reply_markup=None, **kwargs):
        return await self.bot.send_message(self.chat.id, text, reply_markup=reply_markup, **kwargs)

    async def delete(self):
        self.deleted = True
        self.bot.deleted.append((self.chat.id, self.message_id))
        return True


class FakeCallbackQuery:
    def __init__(self, bot: FakeBot, data: str, message: FakeMessage, from_user: FakeUser):
        self.bot = bot
        self.data = data
        self.message = message
        self.from_user = from_user
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[dict[str, Any]] = []
        self.id = str(next(_ids))

    async def answer(self, text=None, show_alert=False, **kwargs):
        from telegram.error import BadRequest

        if self.answers:  # Telegram accepts exactly one answerCallbackQuery per query id
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
        self.answers.append((text, show_alert))
        return True

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup})
        self.message.text = text
        self.message.reply_markup = reply_markup
        self.bot.sent.append({"chat_id": self.message.chat.id, "text": text, "reply_markup": reply_markup, "edit": True})
        return self.message

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.edits.append({"text": None, "reply_markup": reply_markup})
        self.message.reply_markup = reply_markup
        return self.message


@dataclass
class FakeChatMemberUpdated:
    chat: FakeChat
    from_user: FakeUser
    old_status: str
    new_status: str

    @property
    def old_chat_member(self):
        return SimpleNamespace(status=self.old_status)

    @property
    def new_chat_member(self):
        return SimpleNamespace(status=self.new_status)


@dataclass
class FakeUpdate:
    effective_user: FakeUser | None
    effective_chat: FakeChat | None
    message: FakeMessage | None = None
    callback_query: FakeCallbackQuery | None = None
    my_chat_member: FakeChatMemberUpdated | None = None
    update_id: int = field(default_factory=lambda: next(_ids))

    @property
    def effective_message(self):
        if self.message is not None:
            return self.message
        return self.callback_query.message if self.callback_query else None


class FakeContext:
    def __init__(self, bot: FakeBot, bot_data: dict, user_data: dict, args: list[str] | None = None):
        self.bot = bot
        self.bot_data = bot_data
        self.user_data = user_data
        self.args = args or []
        self.error = None
        self.job = None


class BotHarness:
    """Drives handlers the way the Application would, using the real handler registration."""

    def __init__(self, settings, drive, tz=None):
        from zoneinfo import ZoneInfo

        from mg_archive_bot.bot.app import register_handlers

        self.bot = FakeBot()
        self.bot_data = {"settings": settings, "drive": drive, "tz": tz or ZoneInfo(settings.timezone or "UTC")}
        self.user_data: dict[int, dict] = {}
        app = Application.builder().token("123:TEST").build()
        register_handlers(app)
        self.commands: dict[str, Any] = {}
        self.callbacks: list[tuple[Any, Any]] = []
        self.others: list[Any] = []
        for group in sorted(app.handlers):
            for handler in app.handlers[group]:
                if isinstance(handler, CommandHandler):
                    for cmd in handler.commands:
                        self.commands[cmd] = handler.callback
                elif isinstance(handler, CallbackQueryHandler):
                    self.callbacks.append((handler.pattern, handler.callback))
                else:
                    self.others.append(handler)

    def ctx(self, user: FakeUser, args=None) -> FakeContext:
        return FakeContext(self.bot, self.bot_data, self.user_data.setdefault(user.id, {}), args)

    async def command(self, user: FakeUser, text: str, chat: FakeChat | None = None):
        chat = chat or FakeChat(user.id, "private")
        parts = text.split()
        name = parts[0].lstrip("/").split("@")[0]
        msg = FakeMessage(self.bot, chat, text=text, from_user=user)
        update = FakeUpdate(user, chat, message=msg)
        handler = self.commands.get(name)
        if handler is None:
            from mg_archive_bot.bot.handlers.common import unknown_private_command

            handler = unknown_private_command
        await handler(update, self.ctx(user, parts[1:]))
        return update

    async def text(self, user: FakeUser, text: str, chat: FakeChat | None = None):
        from mg_archive_bot.bot.handlers.text_router import route_text

        chat = chat or FakeChat(user.id, "private")
        msg = FakeMessage(self.bot, chat, text=text, from_user=user)
        update = FakeUpdate(user, chat, message=msg)
        await route_text(update, self.ctx(user))
        return update

    async def press(self, user: FakeUser, data: str, chat: FakeChat | None = None, message: FakeMessage | None = None):
        chat = chat or FakeChat(user.id, "private")
        message = message or FakeMessage(self.bot, chat, text="(menu)", from_user=user)
        query = FakeCallbackQuery(self.bot, data, message, user)
        update = FakeUpdate(user, chat, callback_query=query)
        for pattern, callback in self.callbacks:
            if pattern.match(data):
                await callback(update, self.ctx(user))
                return query
        raise AssertionError(f"no callback handler for {data}")

    async def chat_member(self, chat: FakeChat, by: FakeUser, old: str, new: str):
        from mg_archive_bot.bot.handlers.mg_groups import on_my_chat_member

        update = FakeUpdate(by, chat, my_chat_member=FakeChatMemberUpdated(chat, by, old, new))
        await on_my_chat_member(update, self.ctx(by))
        return update

    def buttons(self, markup) -> list[tuple[str, str | None]]:
        if markup is None:
            return []
        return [(b.text, b.callback_data or b.url) for row in markup.inline_keyboard for b in row]
