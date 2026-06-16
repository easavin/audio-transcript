from __future__ import annotations

import logging
import sys

import uvicorn

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


def main() -> None:
    mode = "WEBHOOK" if settings.use_webhook else "POLLING (Telegram)"
    log.info("Starting server on port %s — Telegram mode: %s", settings.port, mode)
    # Import inside main so logging is configured before the app module loads.
    from app.server import app

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Fatal error")
        raise
