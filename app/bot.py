from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
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
from app.prompts import build_summary_prompt, build_translation_prompt
from app.providers.llm import get_llm_provider
from app.providers.stt import get_stt_provider

log = logging.getLogger(__name__)

MODES = ("transcript", "short", "medium", "full")
LANGS = ("auto", "ru", "en", "es")
LANG_LABELS = {"auto": "🌐 Auto", "ru": "🇷🇺 Russian", "en": "🇬🇧 English", "es": "🇪🇸 Spanish"}
MODE_LABELS = {
    "transcript": "📝 Transcript",
    "short": "⚡ Short",
    "medium": "📋 Medium",
    "full": "📖 Full",
}
MAX_TELEGRAM_MSG = 4000
BATCH_WINDOW_SECONDS = 3.0


# ---------- Help / keyboards ----------

def help_text(is_admin: bool) -> str:
    lines = [
        "🎙️ *Voice Transcript Bot*",
        "",
        "Forward me a voice or audio message and I'll transcribe or summarize it.",
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
    return await db.upsert_user(user.id, user.username)


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
        await db.update_mode(s.telegram_id, context.args[0])
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
        await db.update_lang(s.telegram_id, context.args[0])
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
            await db.update_mode(s.telegram_id, value)
            await query.edit_message_text(f"Mode set to: {MODE_LABELS[value]}")
        return
    if data.startswith("l:"):
        value = data[2:]
        if value in LANGS:
            await db.update_lang(s.telegram_id, value)
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


# ---------- Voice handler ----------

async def _send_long(update: Update, text: str) -> None:
    for i in range(0, len(text), MAX_TELEGRAM_MSG):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_MSG])


# ---------- Batch handling ----------
# When several voices arrive within BATCH_WINDOW_SECONDS of each other they are
# combined into a single summary. Keyed by chat_id (one user = one private chat).

@dataclass
class _PendingBatch:
    settings: db.UserSettings
    transcripts: list[str] = field(default_factory=list)
    last_update: Update | None = None
    flush_task: asyncio.Task | None = None


_batches: dict[int, _PendingBatch] = {}


async def _flush_batch_after(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(BATCH_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return

    batch = _batches.get(chat_id)
    if batch is None:
        return
    # Only proceed if we're still the active flush task (no newer voice arrived)
    if batch.flush_task is not asyncio.current_task():
        return
    _batches.pop(chat_id, None)

    if batch.last_update is None or not batch.transcripts:
        return

    if len(batch.transcripts) == 1:
        combined = batch.transcripts[0]
    else:
        combined = "\n\n".join(batch.transcripts)

    await _respond_with(batch.last_update, context, batch.settings, combined)


async def _respond_with(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    s: db.UserSettings,
    transcript: str,
) -> None:
    msg = update.message
    if msg is None:
        return

    if s.default_mode == "transcript":
        if s.default_output_lang == "auto":
            await _send_long(update, transcript)
            return
        await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        prompt = build_translation_prompt(transcript, s.default_output_lang)
        try:
            translated = await get_llm_provider().summarize(prompt)
        except Exception:
            log.exception("Translation failed")
            await msg.reply_text(
                "Translation failed, but here is the original transcript:\n\n" + transcript[:3500]
            )
            return
        await _send_long(update, translated)
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

    # Append to the per-chat batch and (re)schedule the flush.
    chat_id = msg.chat_id
    batch = _batches.get(chat_id)
    if batch is None:
        batch = _PendingBatch(settings=s)
        _batches[chat_id] = batch

    batch.transcripts.append(transcript)
    batch.last_update = update
    batch.settings = s  # user may have changed /mode or /lang between voices

    if batch.flush_task and not batch.flush_task.done():
        batch.flush_task.cancel()
    batch.flush_task = asyncio.create_task(_flush_batch_after(chat_id, context))


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
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    return app
