"""Asset discovery (private chat only): /search, plain-text search, result cards, previews."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ...db import session_scope
from ...models import User
from ...services import notifications
from ...services import projects as project_service
from ...services import search as search_service
from ...services.validation import latest_report
from ...util import esc, normalise_terms
from ..access import require, set_prompt, settings_of
from ..actions import send_preview, tree_of, user_by_id
from ..keyboards import more_results_keyboard, previews_keyboard, search_card_keyboard

log = logging.getLogger(__name__)


async def _send_results(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, page: int) -> None:
    settings = settings_of(context)
    page_size = settings.search_page_size
    chat_id = update.effective_chat.id
    terms = normalise_terms(query)
    with session_scope() as session:
        hits = search_service.search_projects(session, query)
        total = len(hits)
        chunk = hits[page * page_size : (page + 1) * page_size]
        if not chunk:
            text = (
                f"🔍 No projects match <b>{esc(' + '.join(terms))}</b>."
                if page == 0
                else "No more results."
            )
            await context.bot.send_message(chat_id, text)
            return
        if page == 0:
            await context.bot.send_message(
                chat_id, f"🔍 <b>{total} result{'s' if total != 1 else ''}</b> for <b>{esc(' + '.join(terms))}</b>"
            )
        for i, hit in enumerate(chunk):
            is_last = i == len(chunk) - 1
            has_more = (page + 1) * page_size < total
            await context.bot.send_message(
                chat_id,
                notifications.search_result_card(hit.project),
                reply_markup=search_card_keyboard(hit.project),
            )
            if is_last and has_more:
                await context.bot.send_message(
                    chat_id,
                    f"Showing {min((page + 1) * page_size, total)} of {total}.",
                    reply_markup=more_results_keyboard(page, has_more),
                )
    context.user_data["search_query"] = query


@require(scope="private")
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = " ".join(context.args or []).strip()
    if not query:
        set_prompt(context, "search")
        await update.message.reply_text(
            "🔍 Send your search terms, comma-separated.\nExample: <code>worship, gold, particles</code> (all terms must match)."
        )
        return
    await _send_results(update, context, query, 0)


async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    from ..access import clear_prompt

    clear_prompt(context)
    await _send_results(update, context, update.message.text or "", 0)


@require(scope="private")
async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    page = int(query.data.split(":")[1])
    saved = context.user_data.get("search_query")
    if not saved:
        await query.answer("Search again with /search.", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(None)
    except Exception:  # pragma: no cover - cosmetic
        pass
    await _send_results(update, context, saved, page)


@require(scope="private")
async def search_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    _, project_id, action = query.data.split(":")
    with session_scope() as session:
        project = project_service.get_project(session, int(project_id))
        if project is None:
            await query.answer("Project not found.", show_alert=True)
            return
        if action == "prev":
            ready = project.ready_previews
            if not ready:
                await query.answer("No previews available for this project yet.", show_alert=True)
                return
            if len(ready) == 1:
                await query.answer()
                preview_id = ready[0].id
            else:
                await query.answer()
                await query.message.reply_text(
                    f"▶️ <b>{esc(project.full_name)}</b> — {len(ready)} previews on Google Drive. Tap one to play it:",
                    reply_markup=previews_keyboard(project),
                )
                return
        elif action == "details":
            await query.answer()
            report = latest_report(session, project)
            creator = user_by_id(session, project.created_by)
            await query.message.reply_text(
                notifications.project_details(project, report, tree_of(context), context.bot_data["tz"], creator),
                reply_markup=search_card_keyboard(project),
            )
            return
        else:
            await query.answer()
            return
    await send_preview(context, update.effective_chat.id, preview_id)

