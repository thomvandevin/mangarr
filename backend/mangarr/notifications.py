"""Outbound webhook notifications.

Fires a small JSON POST when chapters are imported so a request manager
(e.g. NextPanel) can track availability without waiting for its next poll.
Delivery is fire-and-forget: a dead webhook endpoint must never block or
fail a download.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

APP_NAME = "mangarr"
TIMEOUT = 10.0
HELMARR_QUIET_SECONDS = 60.0


@dataclass
class _HelmarrBatch:
    chapters: int
    label: str
    timer: asyncio.Task


_helmarr_batches: dict[int, _HelmarrBatch] = {}


async def send_webhook(url: str, secret: str, payload: dict) -> bool:
    headers = {"X-Webhook-Secret": secret} if secret else {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError:
        log.warning("webhook delivery failed")
        return False
    if resp.status_code >= 400:
        log.warning("webhook returned HTTP %d", resp.status_code)
        return False
    return True


def notify_import(values: dict[str, str], series_id: int, detail: str = "") -> None:
    """Schedule an import notification if webhooks are configured."""
    if values.get("webhook_enabled") != "true":
        return
    url = values.get("webhook_url", "").strip()
    if not url:
        return
    payload = {
        "app": APP_NAME,
        "event": "import",
        "series_id": series_id,
        "detail": detail,
    }
    asyncio.get_running_loop().create_task(
        send_webhook(url, values.get("webhook_secret", ""), payload)
    )


def notify_helmarr(
    values: dict[str, str], series_id: int, title: str, cover_url: str, label: str, chapters: int = 1
) -> None:
    """Schedule a Helmarr push for imported chapters. Imports of one series
    are batched into a single push, sent once the series has been quiet for
    HELMARR_QUIET_SECONDS, so a backlog doesn't push once per chapter."""
    if values.get("helmarr_enabled") != "true":
        return
    url = values.get("helmarr_url", "").strip()
    if not url:
        return
    batch = _helmarr_batches.get(series_id)
    if batch is not None:
        batch.timer.cancel()
        chapters += batch.chapters
    timer = asyncio.get_running_loop().create_task(
        _send_helmarr_batch(url, series_id, title, cover_url)
    )
    _helmarr_batches[series_id] = _HelmarrBatch(chapters, label, timer)


async def _send_helmarr_batch(url: str, series_id: int, title: str, cover_url: str) -> None:
    await asyncio.sleep(HELMARR_QUIET_SECONDS)
    batch = _helmarr_batches.pop(series_id)
    summary = batch.label if batch.chapters == 1 else f"{batch.chapters} chapters"
    # Helmarr picks the push layout from `source` and has none for Mangarr.
    # Its Tautulli layout shows `title` as a free text line with the poster.
    target = httpx.URL(url).copy_set_param("source", "tautulli")
    await send_webhook(
        str(target), "", {"action": "created", "title": f"{title} - {summary}", "poster_url": cover_url}
    )
