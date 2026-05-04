from __future__ import annotations

import asyncio
import logging
import sys

from app import db
from app.bot import build_application, register_commands
from app.config import settings


def _configure_logging() -> None:
    """INFO/DEBUG → stdout, WARNING+ → stderr.

    Railway colors anything on stderr red, so routing benign info to stdout
    keeps the dashboard readable.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    stdout_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [stdout_handler, stderr_handler]

    # Quiet noisy libraries — only surface their warnings/errors.
    for name in ("httpx", "httpcore", "google_genai", "telegram.ext.Updater"):
        logging.getLogger(name).setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("main")


async def _post_init(app) -> None:
    await db.init_pool()
    log.info("DB pool ready. Admin IDs: %s", settings.admin_ids or "<empty>")
    await register_commands(app)
    log.info("Telegram commands registered")


async def _post_shutdown(_app) -> None:
    await db.close_pool()


def main() -> None:
    app = build_application()
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    if settings.use_webhook:
        url = f"{settings.telegram_webhook_base.rstrip('/')}/{settings.telegram_webhook_secret}"
        log.info("Starting in WEBHOOK mode at %s", url)
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path=settings.telegram_webhook_secret,
            webhook_url=url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=["message"],
        )
    else:
        log.info("Starting in POLLING mode")
        app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error")
        raise
    finally:
        # run_webhook/run_polling handle their own loop; ensure any stray tasks finalize
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
