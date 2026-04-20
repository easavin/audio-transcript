from __future__ import annotations

import asyncio
import logging

from app import db
from app.bot import build_application
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def _post_init(_app) -> None:
    await db.init_pool()
    log.info("DB pool ready. Allowed user IDs: %s", settings.allowed_ids or "<empty>")


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
