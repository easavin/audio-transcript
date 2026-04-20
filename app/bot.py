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
MAX_TELEGRAM_MSG = 4000  # safety under 4096


def _is_authorized(user_id: int) -> bool:
    return user_id in settings.allowed_ids


async def _deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "unknown"
    await update.message.reply_text(
        f"You are not authorized to use this bot.\n"
        f"Your Telegram ID is: `{uid}`\n"
        f"Ask the admin to add it to ALLOWED_USER_IDS.",
        parse_mode="Markdown",
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if not _is_authorized(user.id):
        await _deny(update)
        return
    s = await db.get_or_create_user(user.id, user.username)
    await update.message.reply_text(
        "Hi! Forward me a voice message and I'll transcribe or summarize it.\n\n"
        f"Current mode: *{s.default_mode}*\n"
        f"Output language: *{s.default_output_lang}*\n\n"
        "Commands:\n"
        "  /mode transcript|short|medium|full\n"
        "  /lang auto|ru|en|es\n"
        "  /settings",
        parse_mode="Markdown",
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_authorized(user.id):
        await _deny(update)
        return
    if not context.args or context.args[0] not in MODES:
        await update.message.reply_text(f"Usage: /mode {'|'.join(MODES)}")
        return
    await db.update_mode(user.id, context.args[0])
    await update.message.reply_text(f"Mode set to: {context.args[0]}")


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_authorized(user.id):
        await _deny(update)
        return
    if not context.args or context.args[0] not in LANGS:
        await update.message.reply_text(
            f"Usage: /lang {'|'.join(LANGS)}\n"
            "'auto' keeps the original language of the message."
        )
        return
    await db.update_lang(user.id, context.args[0])
    await update.message.reply_text(f"Output language set to: {context.args[0]}")


async def cmd_settings(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not _is_authorized(user.id):
        await _deny(update)
        return
    s = await db.get_or_create_user(user.id, user.username)
    await update.message.reply_text(
        f"Mode: *{s.default_mode}*\nLanguage: *{s.default_output_lang}*",
        parse_mode="Markdown",
    )


async def _send_long(update: Update, text: str) -> None:
    for i in range(0, len(text), MAX_TELEGRAM_MSG):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_MSG])


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    if not _is_authorized(user.id):
        await _deny(update)
        return

    voice = msg.voice or msg.audio
    if not voice:
        await msg.reply_text("Please send a voice or audio message.")
        return

    s = await db.get_or_create_user(user.id, user.username)
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
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    return app
