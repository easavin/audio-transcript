from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import db
from app.config import settings
from app.prompts import build_summary_prompt
from app.providers.llm import get_llm_provider
from app.providers.stt import get_stt_provider

log = logging.getLogger(__name__)

MODES = ("transcript", "short", "medium", "full")
LANGS = ("auto", "ru", "en", "es")
MAX_TELEGRAM_MSG = 4000


async def _resolve_user(update: Update) -> db.UserSettings | None:
    """Upsert Telegram user into DB and return their settings (or None if no user)."""
    user = update.effective_user
    if not user:
        return None
    return await db.upsert_user(user.id, user.username)


async def _deny_and_show_id(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    await update.message.reply_text(
        "You are not authorized to use this bot yet.\n\n"
        f"Forward this message to the admin:\n\n"
        f"`/grant {uid}`",
        parse_mode="Markdown",
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None:
        return
    if not s.allowed:
        await _deny_and_show_id(update)
        return
    await update.message.reply_text(
        "Hi! Forward me a voice message and I'll transcribe or summarize it.\n\n"
        f"Current mode: *{s.default_mode}*\n"
        f"Output language: *{s.default_output_lang}*\n\n"
        "Commands:\n"
        "  /mode transcript|short|medium|full\n"
        "  /lang auto|ru|en|es\n"
        "  /settings"
        + ("\n\nAdmin:\n  /grant <id>\n  /revoke <id>\n  /users" if s.is_admin else ""),
        parse_mode="Markdown",
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    if not context.args or context.args[0] not in MODES:
        await update.message.reply_text(f"Usage: /mode {'|'.join(MODES)}")
        return
    await db.update_mode(s.telegram_id, context.args[0])
    await update.message.reply_text(f"Mode set to: {context.args[0]}")


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    if not context.args or context.args[0] not in LANGS:
        await update.message.reply_text(
            f"Usage: /lang {'|'.join(LANGS)}\n'auto' keeps the transcript's original language."
        )
        return
    await db.update_lang(s.telegram_id, context.args[0])
    await update.message.reply_text(f"Output language set to: {context.args[0]}")


async def cmd_settings(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    await update.message.reply_text(
        f"Mode: *{s.default_mode}*\nLanguage: *{s.default_output_lang}*",
        parse_mode="Markdown",
    )


def _parse_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.is_admin:
        await update.message.reply_text("Admin only.")
        return
    target = _parse_id(context.args or [])
    if target is None:
        await update.message.reply_text("Usage: /grant <telegram_user_id>")
        return
    # Ensure the target exists in DB even if they've never hit /start
    existed = await db.get_user(target) is not None
    if not existed:
        await db.upsert_user(target, None)
    ok = await db.grant(target)
    if ok:
        await update.message.reply_text(
            f"Granted access to {target}." + ("" if existed else " (They haven't opened the bot yet.)")
        )
    else:
        await update.message.reply_text(f"Could not grant {target}.")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.is_admin:
        await update.message.reply_text("Admin only.")
        return
    target = _parse_id(context.args or [])
    if target is None:
        await update.message.reply_text("Usage: /revoke <telegram_user_id>")
        return
    ok = await db.revoke(target)
    if ok:
        await update.message.reply_text(f"Revoked access from {target}.")
    else:
        await update.message.reply_text(
            f"{target} was not a regular allowed user (admins cannot be revoked this way)."
        )


async def cmd_users(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.is_admin:
        await update.message.reply_text("Admin only.")
        return
    users = await db.list_allowed()
    if not users:
        await update.message.reply_text("No allowed users.")
        return
    lines = [
        f"{'⭐' if u.is_admin else '•'} `{u.telegram_id}`"
        + (f" @{u.username}" if u.username else "")
        for u in users
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _send_long(update: Update, text: str) -> None:
    for i in range(0, len(text), MAX_TELEGRAM_MSG):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_MSG])


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    msg = update.message
    if s is None or not msg:
        return
    if not s.allowed:
        await _deny_and_show_id(update)
        return

    voice = msg.voice or msg.audio
    if not voice:
        await msg.reply_text("Please send a voice or audio message.")
        return

    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"audio-{voice.file_unique_id}.ogg"
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(path)

        try:
            transcript = await get_stt_provider().transcribe(path)
        except Exception:
            log.exception("STT failed")
            await msg.reply_text("Transcription failed. Try again or send a shorter clip.")
            return

    if not transcript:
        await msg.reply_text("I couldn't detect any speech in that message.")
        return

    if s.default_mode == "transcript":
        await _send_long(update, transcript)
        return

    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    prompt = build_summary_prompt(transcript, s.default_mode, s.default_output_lang)
    try:
        summary = await get_llm_provider().summarize(prompt)
    except Exception:
        log.exception("LLM failed")
        await msg.reply_text(
            "Summary failed, but here is the raw transcript:\n\n" + transcript[:3500]
        )
        return

    await _send_long(update, summary)


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    return app
