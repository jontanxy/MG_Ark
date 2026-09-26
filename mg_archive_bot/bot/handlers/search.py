"""Asset discovery (private chat only): /search, plain-text search, result cards, previews."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram.error import BadRequest

from ...constants import ProjectStatus, Role
from ...db import session_scope
from ...models import User
from ...services import notifications
from ...services import projects as project_service
from ...services import search as search_service
from ...services.validation import latest_report
from ...util import esc, normalise_terms
from ..access import parse_int, require, set_prompt, settings_of
from ..actions import may_list_files, project_file_listing, send_preview, tree_of, user_by_id
from ..keyboards import more_results_keyboard, previews_keyboard, search_card_keyboard

log = logging.getLogger(__name__)


LIGHTS_ONLY_PREVIEW = "Lights access is preview-only."


def _view_only(actor: User) -> bool:
    return actor.role == Role.LIGHTS


async def _send_results(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, page: int, actor: User) -> None:
    settings = settings_of(context)
    page_size = settings.search_page_size
    chat_id = update.effective_chat.id
    terms = normalise_terms(query)
    shown = " + ".join(terms)
    if len(shown) > 200:
        shown = shown[:200] + "…"
    with session_scope() as session:
        hits = search_service.search_projects(session, query)
        total = len(hits)
        chunk = hits[page * page_size : (page + 1) * page_size]
        if not chunk:
            text = (
                f"🔍 No projects match <b>{esc(shown)}</b>."
                if page == 0
                else "No more results."
            )
            await context.bot.send_message(chat_id, text)
            return
        if page == 0:
            await context.bot.send_message(
                chat_id, f"🔍 <b>{total} result{'s' if total != 1 else ''}</b> for <b>{esc(shown)}</b>"
            )
        for i, hit in enumerate(chunk):
            is_last = i == len(chunk) - 1
            has_more = (page + 1) * page_size < total
            await context.bot.send_message(
                chat_id,
                notifications.search_result_card(hit.project),
                reply_markup=search_card_keyboard(hit.project, view_only=_view_only(actor)),
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
    await _send_results(update, context, query, 0, actor)


async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    from ..access import clear_prompt

    clear_prompt(context)
    await _send_results(update, context, update.message.text or "", 0, actor)


@require(scope="private")
async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    page = parse_int(query.data.split(":")[1])
    if page is None or page > 10_000:
        await query.answer("Invalid request.", show_alert=True)
        return
    saved = context.user_data.get("search_query")
    if not saved:
        await query.answer("Search again with /search.", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest as exc:  # cosmetic: the "more" button may already be gone
        log.debug("could not remove pagination button: %s", exc)
    await _send_results(update, context, saved, page, actor)


@require(scope="private")
async def search_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: User) -> None:
    query = update.callback_query
    _, raw_id, action = query.data.split(":")
    project_id = parse_int(raw_id)
    if project_id is None:
        await query.answer("Invalid request.", show_alert=True)
        return
    with session_scope() as session:
        project = project_service.get_project(session, project_id)
        if project is None or project.status in (ProjectStatus.DRAFT, ProjectStatus.CANCELLED):
            await query.answer("Project not found.", show_alert=True)  # drafts/cancelled are not searchable either
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
        elif action in ("files", "details") and _view_only(actor):
            await query.answer(LIGHTS_ONLY_PREVIEW, show_alert=True)  # crafted button data gets nothing extra
            return
        elif action == "files":
            if not may_list_files(context, actor.telegram_id):
                await query.answer("Please wait a minute before requesting another file listing.", show_alert=True)
                return
            await query.answer("Reading Google Drive…")
            for chunk in await project_file_listing(context, project):
                await query.message.reply_text(chunk)
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

