"""WhatsApp adapter: turns Cloud API webhook events into bot actions.

Mirrors the Telegram feature set — modes, languages, per-user settings, and the
admin allowlist — using WhatsApp interactive list/button messages in place of
Telegram inline keyboards.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

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
from app.providers.stt import get_stt_provider
from app.providers.whatsapp import WhatsAppClient

log = logging.getLogger(__name__)

_client: WhatsAppClient | None = None
_batcher: Batcher | None = None


def init() -> None:
    """Build the client + batcher once the config is known. Call at startup."""
    global _client, _batcher
    _client = WhatsAppClient(
        settings.whatsapp_access_token,
        settings.whatsapp_phone_number_id,
        settings.graph_api_version,
    )
    _batcher = Batcher(_flush)


def _wa() -> WhatsAppClient:
    assert _client is not None, "whatsapp_handler.init() not called"
    return _client


# ---------- Help / keyboards ----------

def _help_text(is_admin: bool) -> str:
    lines = [
        "🎙️ *Voice Transcript Bot*",
        "",
        "Send me a voice message and I'll transcribe or summarize it.",
        "",
        "Modes: transcript / short / medium / full",
        "Languages: auto / ru / en / es",
        "",
        "Tip: send several voices in a row and I'll combine them.",
        "",
        "Commands (type them as text):",
        "mode — pick output style",
        "lang — pick output language",
        "settings — show current settings",
        "help — this message",
    ]
    if is_admin:
        lines += [
            "",
            "Admin:",
            "grant <number> — allow a user",
            "revoke <number> — remove access",
            "users — list allowed users",
        ]
    return "\n".join(lines)


async def _send_mode_picker(to: str, current: str) -> None:
    rows = [
        (f"m:{m}", ("✅ " if m == current else "") + MODE_LABELS[m], "")
        for m in MODES
    ]
    await _wa().send_list(to, "Pick output style:", "Choose mode", rows)


async def _send_lang_picker(to: str, current: str) -> None:
    rows = [
        (f"l:{l}", ("✅ " if l == current else "") + LANG_LABELS[l], "")
        for l in LANGS
    ]
    await _wa().send_list(to, "Pick output language:", "Choose language", rows)


async def _send_settings(to: str, s: db.UserSettings) -> None:
    await _wa().send_text(
        to,
        f"Mode: {MODE_LABELS[s.default_mode]}\nLanguage: {LANG_LABELS[s.default_output_lang]}",
    )
    await _wa().send_buttons(
        to, "Change a setting:", [("open:mode", "⚙️ Mode"), ("open:lang", "🌐 Language")]
    )


async def _send_long(to: str, text: str) -> None:
    for part in chunk(text):
        await _wa().send_text(to, part)


# ---------- Webhook entry ----------

async def handle_event(payload: dict) -> None:
    """Top-level handler for a verified POST /whatsapp body."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {
                c.get("wa_id"): c.get("profile", {}).get("name")
                for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                try:
                    await _handle_message(msg, contacts)
                except Exception:
                    log.exception("Failed handling WhatsApp message")
                    wa_id = msg.get("from")
                    if wa_id:
                        await _safe_text(
                            wa_id, "Something went wrong on my side. Please try again."
                        )


async def _safe_text(to: str, body: str) -> None:
    try:
        await _wa().send_text(to, body)
    except Exception:
        log.exception("Failed to send WhatsApp text to %s", to)


async def _handle_message(msg: dict, contacts: dict) -> None:
    wa_id = msg["from"]
    name = contacts.get(wa_id)
    s = await db.upsert_user("whatsapp", wa_id, name)

    if not s.allowed:
        await _wa().send_text(
            wa_id,
            "You are not authorized to use this bot yet.\n\n"
            f"Ask the admin to grant access to: {wa_id}",
        )
        return

    await _wa().mark_read(msg["id"])
    mtype = msg.get("type")

    if mtype in ("audio", "voice"):
        await _handle_audio(msg, s, wa_id)
    elif mtype == "text":
        await _handle_text(msg["text"]["body"].strip(), s, wa_id)
    elif mtype == "interactive":
        await _handle_interactive(msg["interactive"], s, wa_id)
    else:
        await _wa().send_text(wa_id, "Send me a voice message, or type 'help'.")


# ---------- Audio ----------

async def _handle_audio(msg: dict, s: db.UserSettings, wa_id: str) -> None:
    media = msg.get("audio") or msg.get("voice")
    if not media:
        await _wa().send_text(wa_id, "Please send a voice or audio message.")
        return
    media_id = media["id"]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"audio-{media_id}.ogg"
        try:
            await _wa().download_media(media_id, path)
        except Exception:
            log.exception("WhatsApp media download failed")
            await _wa().send_text(
                wa_id, "I couldn't fetch that audio from WhatsApp. Please try again."
            )
            return
        try:
            transcript = await get_stt_provider().transcribe(path)
        except Exception:
            log.exception("STT failed")
            await _wa().send_text(
                wa_id, "Transcription failed. Try again or send a shorter clip."
            )
            return

    if not transcript:
        await _wa().send_text(wa_id, "I couldn't detect any speech in that message.")
        return

    assert _batcher is not None
    _batcher.add(wa_id, s, transcript, wa_id)


async def _flush(to: str, s: db.UserSettings, combined: str) -> None:
    try:
        text = await transcript_to_reply(s, combined)
    except Exception:
        log.exception("LLM failed")
        await _send_long(
            to, "Summary failed, but here is the raw transcript:\n\n" + combined[:3500]
        )
        return
    await _send_long(to, text)


# ---------- Text commands ----------

async def _handle_text(text: str, s: db.UserSettings, wa_id: str) -> None:
    parts = text.split()
    if not parts:
        await _wa().send_text(wa_id, _help_text(s.is_admin))
        return
    cmd = parts[0].lstrip("/").lower()
    args = parts[1:]

    if cmd in ("start", "help", "hi", "hello"):
        await _wa().send_text(wa_id, _help_text(s.is_admin))
        await _wa().send_buttons(
            wa_id, "Change a setting:", [("open:mode", "⚙️ Mode"), ("open:lang", "🌐 Language")]
        )
    elif cmd == "mode":
        if args and args[0] in MODES:
            await db.update_mode("whatsapp", wa_id, args[0])
            await _wa().send_text(wa_id, f"Mode set to: {MODE_LABELS[args[0]]}")
        else:
            await _send_mode_picker(wa_id, s.default_mode)
    elif cmd in ("lang", "language"):
        if args and args[0] in LANGS:
            await db.update_lang("whatsapp", wa_id, args[0])
            await _wa().send_text(wa_id, f"Language set to: {LANG_LABELS[args[0]]}")
        else:
            await _send_lang_picker(wa_id, s.default_output_lang)
    elif cmd == "settings":
        await _send_settings(wa_id, s)
    elif cmd == "grant":
        await _admin_grant(args, s, wa_id)
    elif cmd == "revoke":
        await _admin_revoke(args, s, wa_id)
    elif cmd == "users":
        await _admin_users(s, wa_id)
    else:
        await _wa().send_text(wa_id, "Send me a voice message, or type 'help'.")


# ---------- Interactive replies ----------

async def _handle_interactive(interactive: dict, s: db.UserSettings, wa_id: str) -> None:
    reply = interactive.get("list_reply") or interactive.get("button_reply")
    if not reply:
        return
    data = reply.get("id", "")

    if data == "open:mode":
        await _send_mode_picker(wa_id, s.default_mode)
    elif data == "open:lang":
        await _send_lang_picker(wa_id, s.default_output_lang)
    elif data.startswith("m:") and data[2:] in MODES:
        await db.update_mode("whatsapp", wa_id, data[2:])
        await _wa().send_text(wa_id, f"Mode set to: {MODE_LABELS[data[2:]]}")
    elif data.startswith("l:") and data[2:] in LANGS:
        await db.update_lang("whatsapp", wa_id, data[2:])
        await _wa().send_text(wa_id, f"Language set to: {LANG_LABELS[data[2:]]}")


# ---------- Admin ----------

def _normalize_number(raw: str) -> str:
    """Strip everything but digits so '+1 (555) 123' matches the wa_id form."""
    return "".join(ch for ch in raw if ch.isdigit())


async def _admin_grant(args: list[str], s: db.UserSettings, wa_id: str) -> None:
    if not s.is_admin:
        await _wa().send_text(wa_id, "Admin only.")
        return
    if not args:
        await _wa().send_text(wa_id, "Usage: grant <whatsapp_number>")
        return
    target = _normalize_number(args[0])
    existed = await db.get_user("whatsapp", target) is not None
    if not existed:
        await db.upsert_user("whatsapp", target, None)
    ok = await db.grant("whatsapp", target)
    if ok:
        await _wa().send_text(
            wa_id,
            f"Granted access to {target}."
            + ("" if existed else " (They haven't messaged the bot yet.)"),
        )
    else:
        await _wa().send_text(wa_id, f"Could not grant {target}.")


async def _admin_revoke(args: list[str], s: db.UserSettings, wa_id: str) -> None:
    if not s.is_admin:
        await _wa().send_text(wa_id, "Admin only.")
        return
    if not args:
        await _wa().send_text(wa_id, "Usage: revoke <whatsapp_number>")
        return
    target = _normalize_number(args[0])
    ok = await db.revoke("whatsapp", target)
    if ok:
        await _wa().send_text(wa_id, f"Revoked access from {target}.")
    else:
        await _wa().send_text(
            wa_id,
            f"{target} was not a regular allowed user (admins cannot be revoked this way).",
        )


async def _admin_users(s: db.UserSettings, wa_id: str) -> None:
    if not s.is_admin:
        await _wa().send_text(wa_id, "Admin only.")
        return
    users = await db.list_allowed("whatsapp")
    if not users:
        await _wa().send_text(wa_id, "No allowed users.")
        return
    lines = [
        f"{'⭐' if u.is_admin else '•'} {u.external_id}"
        + (f" ({u.username})" if u.username else "")
        for u in users
    ]
    await _wa().send_text(wa_id, "\n".join(lines))
