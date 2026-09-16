"""Routes free-text private messages to whichever prompt is pending for the user."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ...constants import UserStatus
from ..access import clear_prompt, get_prompt, is_private, may_reply_to_unregistered, resolve_actor
from . import admin, auth, projects, search

log = logging.getLogger(__name__)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update) or update.message is None or not update.message.text:
        return
    prompt = get_prompt(context)
    if prompt and prompt.get("kind") == "password":
        await auth.handle_password(update, context)
        return
    actor = resolve_actor(update, context)
    if actor is None:
        if may_reply_to_unregistered(context, update.effective_user.id):
            await update.message.reply_text("Send /start to register with the access password.")
        return
    if actor.status != UserStatus.ACTIVE:
        clear_prompt(context)
        await update.message.reply_text("🚫 Your access has been revoked. Contact the Super Admin.")
        return
    kind = prompt.get("kind") if prompt else None
    if kind == "expired":
        await update.message.reply_text("That step expired. Send the command again to continue.")
        return
    if kind == "new_password":
        await admin.handle_new_password(update, context, actor)
    elif kind == "project_name":
        await projects.handle_project_name(update, context, actor)
    elif kind == "collection_name":
        await projects.handle_collection_name(update, context, actor)
    elif kind == "wizard_meta":
        await projects.handle_wizard_meta(update, context, actor)
    elif kind == "meta_value":
        await projects.handle_meta_value(update, context, actor)
    elif kind == "search":
        await search.handle_search_text(update, context, actor)
    else:
        # No prompt pending: treat plain text as a search query.
        await search.handle_search_text(update, context, actor)
