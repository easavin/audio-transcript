"""Thin async wrapper over the WhatsApp Cloud (Graph) API.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# WhatsApp interactive limits.
_BTN_TITLE_MAX = 20
_ROW_TITLE_MAX = 24
_ROW_DESC_MAX = 72


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class WhatsAppClient:
    def __init__(
        self, access_token: str, phone_number_id: str, api_version: str = "v21.0"
    ) -> None:
        self._base = f"https://graph.facebook.com/{api_version}"
        self._phone_id = phone_number_id
        self._auth = {"Authorization": f"Bearer {access_token}"}

    # ---------- Inbound media ----------

    async def download_media(self, media_id: str, dest: Path, timeout: float = 120.0) -> None:
        """Resolve a media id to its CDN URL and download the bytes to `dest`.

        Both the lookup and the download require the bearer token.
        """
        async with httpx.AsyncClient(timeout=timeout, headers=self._auth) as client:
            meta = await client.get(f"{self._base}/{media_id}")
            meta.raise_for_status()
            url = meta.json()["url"]
            resp = await client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)

    # ---------- Outbound ----------

    async def _post(self, payload: dict, timeout: float = 30.0) -> None:
        async with httpx.AsyncClient(timeout=timeout, headers=self._auth) as client:
            resp = await client.post(f"{self._base}/{self._phone_id}/messages", json=payload)
            if resp.status_code >= 400:
                log.error("WhatsApp send failed %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()

    async def send_text(self, to: str, body: str) -> None:
        await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            }
        )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        """Reply buttons — max 3. `buttons` is a list of (id, title)."""
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": bid, "title": _clip(title, _BTN_TITLE_MAX)}}
                            for bid, title in buttons[:3]
                        ]
                    },
                },
            }
        )

    async def send_list(
        self, to: str, body: str, button_label: str, rows: list[tuple[str, str, str]]
    ) -> None:
        """List message — up to 10 rows. `rows` is a list of (id, title, description)."""
        await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {
                        "button": _clip(button_label, _BTN_TITLE_MAX),
                        "sections": [
                            {
                                "rows": [
                                    {
                                        "id": rid,
                                        "title": _clip(title, _ROW_TITLE_MAX),
                                        "description": _clip(desc, _ROW_DESC_MAX),
                                    }
                                    for rid, title, desc in rows[:10]
                                ]
                            }
                        ],
                    },
                },
            }
        )

    async def mark_read(self, message_id: str) -> None:
        try:
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                }
            )
        except Exception:
            # Read receipts are best-effort; never let them break message handling.
            log.debug("mark_read failed for %s", message_id, exc_info=True)
