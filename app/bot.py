from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import db
from app.config import settings
from app.core import (
    LANG_LABELS,
    LANGS,
    MODE_LABELS,
    MODES,
    Batcher,
    chunk,
    transcript_to_reply,
)
from app.prompts import build_error_explanation_prompt
from app.providers.llm import get_llm_provider
from app.providers.stt import get_stt_provider

log = logging.getLogger(__name__)

PLATFORM = "telegram"


# ---------- Help / keyboards ----------

def help_text(is_admin: bool) -> str:
    lines = [
        "🎙️ *Voice Transcript Bot*",
        "",
        "Forward me a voice, audio, or video message (including round video notes) and I'll transcribe or summarize it.",
        "",
        "*Modes:*",
        "• 📝 *Transcript* — full text (translated if you pick a language)",
        "• ⚡ *Short* — 1–2 sentence summary",
        "• 📋 *Medium* — 3–5 bullet points",
        "• 📖 *Full* — organized summary with all key details _(default)_",
        "",
        "*Language:*",
        "• 🌐 *Auto* — keep the original language of the message",
        "• Or pick Russian / English / Spanish — the output is translated into it",
        "",
        "_Tip: send several voices in a row and I'll combine them into one summary._",
        "",
        "*Commands:*",
        "/mode — pick output style",
        "/lang — pick output language",
        "/settings — show current settings",
        "/help — this message",
    ]
    if is_admin:
        lines += [
            "",
            "*Admin:*",
            "/grant `<id>` — allow a user",
            "/revoke `<id>` — remove access",
            "/users — list allowed users",
        ]
    return "\n".join(lines)


def _mode_keyboard(current: str) -> InlineKeyboardMarkup:
    def label(m: str) -> str:
        return ("✅ " if m == current else "") + MODE_LABELS[m]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("transcript"), callback_data="m:transcript"),
         InlineKeyboardButton(label("short"), callback_data="m:short")],
        [InlineKeyboardButton(label("medium"), callback_data="m:medium"),
         InlineKeyboardButton(label("full"), callback_data="m:full")],
    ])


def _lang_keyboard(current: str) -> InlineKeyboardMarkup:
    def label(l: str) -> str:
        return ("✅ " if l == current else "") + LANG_LABELS[l]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label("auto"), callback_data="l:auto"),
         InlineKeyboardButton(label("ru"), callback_data="l:ru")],
        [InlineKeyboardButton(label("en"), callback_data="l:en"),
         InlineKeyboardButton(label("es"), callback_data="l:es")],
    ])


def _settings_shortcut_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚙️ Mode", callback_data="open:mode"),
        InlineKeyboardButton("🌐 Language", callback_data="open:lang"),
    ]])


# ---------- Auth helpers ----------

async def _resolve_user(update: Update) -> db.UserSettings | None:
    user = update.effective_user
    if not user:
        return None
    return await db.upsert_user(PLATFORM, str(user.id), user.username)


async def _deny_and_show_id(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    await update.message.reply_text(
        "You are not authorized to use this bot yet.\n\n"
        f"Forward this message to the admin:\n\n`/grant {uid}`",
        parse_mode="Markdown",
    )


# ---------- Commands ----------

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None:
        return
    if not s.allowed:
        await _deny_and_show_id(update)
        return
    text = (
        help_text(s.is_admin)
        + f"\n\n_Current:_ {MODE_LABELS[s.default_mode]}  •  {LANG_LABELS[s.default_output_lang]}"
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_settings_shortcut_keyboard(),
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    await update.message.reply_text(
        help_text(s.is_admin),
        parse_mode="Markdown",
        reply_markup=_settings_shortcut_keyboard(),
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    if context.args and context.args[0] in MODES:
        await db.update_mode(PLATFORM, s.external_id, context.args[0])
        await update.message.reply_text(f"Mode set to: {MODE_LABELS[context.args[0]]}")
        return
    await update.message.reply_text(
        "Pick output style:", reply_markup=_mode_keyboard(s.default_mode)
    )


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    if context.args and context.args[0] in LANGS:
        await db.update_lang(PLATFORM, s.external_id, context.args[0])
        await update.message.reply_text(f"Language set to: {LANG_LABELS[context.args[0]]}")
        return
    await update.message.reply_text(
        "Pick output language:", reply_markup=_lang_keyboard(s.default_output_lang)
    )


async def cmd_settings(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await _deny_and_show_id(update)
        return
    await update.message.reply_text(
        f"*Mode:* {MODE_LABELS[s.default_mode]}\n"
        f"*Language:* {LANG_LABELS[s.default_output_lang]}",
        parse_mode="Markdown",
        reply_markup=_settings_shortcut_keyboard(),
    )


# ---------- Callback button handler ----------

async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    s = await _resolve_user(update)
    if s is None or not s.allowed:
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "open:mode":
        await query.edit_message_text(
            "Pick output style:", reply_markup=_mode_keyboard(s.default_mode)
        )
        return
    if data == "open:lang":
        await query.edit_message_text(
            "Pick output language:", reply_markup=_lang_keyboard(s.default_output_lang)
        )
        return

    if data.startswith("m:"):
        value = data[2:]
        if value in MODES:
            await db.update_mode(PLATFORM, s.external_id, value)
            await query.edit_message_text(f"Mode set to: {MODE_LABELS[value]}")
        return
    if data.startswith("l:"):
        value = data[2:]
        if value in LANGS:
            await db.update_lang(PLATFORM, s.external_id, value)
            await query.edit_message_text(f"Language set to: {LANG_LABELS[value]}")
        return


# ---------- Admin commands ----------

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
    existed = await db.get_user(PLATFORM, str(target)) is not None
    if not existed:
        await db.upsert_user(PLATFORM, str(target), None)
    ok = await db.grant(PLATFORM, str(target))
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
    ok = await db.revoke(PLATFORM, str(target))
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
    users = await db.list_allowed(PLATFORM)
    if not users:
        await update.message.reply_text("No allowed users.")
        return
    lines = [
        f"{'⭐' if u.is_admin else '•'} `{u.external_id}`"
        + (f" @{u.username}" if u.username else "")
        for u in users
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- Voice handler ----------

async def _send_long(update: Update, text: str) -> None:
    for part in chunk(text):
        await update.message.reply_text(part)


async def _flush_telegram(
    payload: tuple[Update, ContextTypes.DEFAULT_TYPE],
    s: db.UserSettings,
    combined: str,
) -> None:
    update, context = payload
    msg = update.message
    if msg is None:
        return
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    try:
        text = await transcript_to_reply(s, combined)
    except Exception:
        log.exception("LLM failed")
        verb = "Translation" if s.default_mode == "transcript" else "Summary"
        await msg.reply_text(
            f"{verb} failed, but here is the raw transcript:\n\n" + combined[:3500]
        )
        return
    await _send_long(update, text)


_batcher = Batcher(_flush_telegram)


_DOWNLOAD_ATTEMPTS = 3


async def _download_voice(
    context: ContextTypes.DEFAULT_TYPE, file_id: str, path: Path
) -> None:
    """Fetch a voice/audio file from Telegram's CDN with a few bounded retries.

    The CDN occasionally stalls; a single attempt under a tight timeout is the
    main source of the 'it took too long' errors users were seeing.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(path)
            return
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            log.warning(
                "Telegram download attempt %d/%d failed: %s",
                attempt,
                _DOWNLOAD_ATTEMPTS,
                exc,
            )
            if attempt < _DOWNLOAD_ATTEMPTS:
                await asyncio.sleep(1.0 * attempt)
    assert last_exc is not None
    raise last_exc


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _resolve_user(update)
    msg = update.message
    if s is None or not msg:
        return
    if not s.allowed:
        await _deny_and_show_id(update)
        return

    # Circle video notes and regular videos carry an audio track Whisper can
    # transcribe directly; voice notes use .ogg, everything video-ish is .mp4.
    media = msg.voice or msg.audio or msg.video_note or msg.video
    if not media:
        await msg.reply_text("Please send a voice, audio, or video message.")
        return

    ext = "ogg" if (msg.voice or msg.audio) else "mp4"

    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"audio-{media.file_unique_id}.{ext}"
        try:
            await _download_voice(context, media.file_id, path)
        except (TimedOut, NetworkError):
            log.exception("Telegram download failed")
            await msg.reply_text(
                "I couldn't fetch that audio from Telegram. Please try sending it again."
            )
            return

        try:
            transcript = await get_stt_provider().transcribe(path)
        except Exception:
            log.exception("STT failed")
            await msg.reply_text("Transcription failed. Try again or send a shorter clip.")
            return

    if not transcript:
        await msg.reply_text("I couldn't detect any speech in that message.")
        return

    _batcher.add(str(msg.chat_id), s, transcript, (update, context))


# ---------- Global error handler ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.exception("Unhandled exception in handler", exc_info=err)

    if not isinstance(update, Update) or update.effective_message is None:
        return

    lang_hint = (
        update.effective_user.language_code
        if update.effective_user and update.effective_user.language_code
        else "en"
    )
    error_text = f"{type(err).__name__}: {err}" if err else "Unknown error"

    explanation: str | None = None
    try:
        prompt = build_error_explanation_prompt(error_text, lang_hint)
        explanation = await get_llm_provider().summarize(prompt)
    except Exception:
        log.exception("LLM-based error explanation failed")

    if not explanation:
        explanation = "Something went wrong on my side. Please try again in a moment."

    try:
        await update.effective_message.reply_text(explanation)
    except Exception:
        log.exception("Failed to deliver error explanation to user")


USER_COMMANDS = [
    BotCommand("start", "Welcome + how it works"),
    BotCommand("help", "Show help"),
    BotCommand("mode", "Pick output style"),
    BotCommand("lang", "Pick output language"),
    BotCommand("settings", "Show current settings"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("grant", "Allow a user"),
    BotCommand("revoke", "Remove a user"),
    BotCommand("users", "List allowed users"),
]


async def register_commands(app: Application) -> None:
    """Publish commands to Telegram so they show in the / autocomplete menu."""
    await app.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_ids:
        try:
            await app.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            log.exception("Failed to set admin commands for %s", admin_id)


def build_application() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connect_timeout(settings.telegram_connect_timeout)
        .read_timeout(settings.telegram_read_timeout)
        .write_timeout(settings.telegram_write_timeout)
        .media_write_timeout(settings.telegram_write_timeout)
        .pool_timeout(settings.telegram_pool_timeout)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE | filters.VIDEO,
            on_voice,
        )
    )
    app.add_error_handler(on_error)
    return app
