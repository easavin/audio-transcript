"""Single FastAPI process hosting BOTH webhooks:

  POST /telegram/{secret}  -> fed into the python-telegram-bot Application
  GET  /whatsapp           -> Meta webhook verification handshake
  POST /whatsapp           -> WhatsApp Cloud API events

The Telegram Application is initialized/started in the lifespan and driven by
process_update() rather than its own web server, so both platforms share one
event loop, one DB pool, and one Railway service.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import FastAPI, Request, Response
from telegram import Update

from app import db, whatsapp_handler
from app.bot import build_application, register_commands
from app.config import settings

log = logging.getLogger(__name__)

_TELEGRAM_UPDATES = ["message", "callback_query"]

ptb_app = build_application()


async def _startup() -> None:
    await db.init_pool()
    log.info("DB pool ready. Telegram admins: %s", settings.admin_ids or "<empty>")
    if settings.whatsapp_admin_set:
        log.info("WhatsApp admins: %s", settings.whatsapp_admin_set)

    await ptb_app.initialize()
    await ptb_app.start()

    if settings.use_webhook:
        url = f"{settings.telegram_webhook_base.rstrip('/')}/telegram/{settings.telegram_webhook_secret}"
        await ptb_app.bot.set_webhook(
            url=url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=_TELEGRAM_UPDATES,
            drop_pending_updates=False,
        )
        log.info("Telegram webhook set: %s", url)
    else:
        await ptb_app.updater.start_polling(allowed_updates=_TELEGRAM_UPDATES)
        log.info("Telegram polling started (no webhook base configured)")

    await register_commands(ptb_app)

    if settings.whatsapp_enabled:
        whatsapp_handler.init()
        log.info(
            "WhatsApp enabled. Webhook: %s/whatsapp",
            settings.telegram_webhook_base.rstrip("/") or "<set TELEGRAM_WEBHOOK_BASE>",
        )
    else:
        log.warning("WhatsApp NOT enabled — missing access token / phone id / verify token")


async def _shutdown() -> None:
    if not settings.use_webhook and ptb_app.updater and ptb_app.updater.running:
        await ptb_app.updater.stop()
    await ptb_app.stop()
    await ptb_app.shutdown()
    await db.close_pool()


app = FastAPI(on_startup=[_startup], on_shutdown=[_shutdown])


@app.get("/")
async def health() -> dict:
    return {"ok": True}


# ---------- Telegram ----------

@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    if not hmac.compare_digest(secret, settings.telegram_webhook_secret):
        return Response(status_code=403)
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(header, settings.telegram_webhook_secret):
        return Response(status_code=403)

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)


# ---------- WhatsApp ----------

@app.get("/whatsapp")
async def whatsapp_verify(request: Request) -> Response:
    """Meta calls this once with hub.challenge when you save the webhook."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.whatsapp_verify_token
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


def _valid_signature(raw: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256 using the Meta App Secret.

    If no app secret is configured we skip the check (useful for first-run/testing),
    but configuring it is strongly recommended in production.
    """
    if not settings.whatsapp_app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.whatsapp_app_secret.encode(), raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@app.post("/whatsapp")
async def whatsapp_event(request: Request) -> Response:
    raw = await request.body()
    if not _valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("WhatsApp event with invalid signature rejected")
        return Response(status_code=403)

    # Always 200 quickly so Meta doesn't retry; handling errors are logged inside.
    if settings.whatsapp_enabled:
        payload = await request.json()
        await whatsapp_handler.handle_event(payload)
    return Response(status_code=200)
