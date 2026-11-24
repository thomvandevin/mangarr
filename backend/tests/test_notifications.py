import asyncio
import json

import pytest
import respx
from httpx import Response

from mangarr import notifications


@respx.mock
async def test_send_webhook_posts_secret_header():
    route = respx.post("http://panel.test/api/v1/webhooks/mangarr").mock(
        return_value=Response(204)
    )
    ok = await notifications.send_webhook(
        "http://panel.test/api/v1/webhooks/mangarr", "s3cret",
        {"app": "mangarr", "event": "import", "series_id": 7},
    )
    assert ok is True
    request = route.calls[0].request
    assert request.headers["x-webhook-secret"] == "s3cret"
    assert b'"series_id": 7' in request.content or b'"series_id":7' in request.content


@respx.mock
async def test_send_webhook_swallows_errors():
    respx.post("http://panel.test/hook").mock(return_value=Response(401))
    ok = await notifications.send_webhook("http://panel.test/hook", "", {"event": "import"})
    assert ok is False


@respx.mock
async def test_notify_import_fire_and_forget():
    route = respx.post("http://panel.test/hook").mock(return_value=Response(204))
    values = {
        "webhook_enabled": "true",
        "webhook_url": "http://panel.test/hook",
        "webhook_secret": "s",
    }
    notifications.notify_import(values, 42, "Chapter 12")
    await asyncio.sleep(0.05)
    assert route.called


async def test_notify_import_disabled_schedules_nothing():
    # would raise if it tried to reach the (unmocked) network
    notifications.notify_import({"webhook_enabled": "false", "webhook_url": "http://x"}, 1)
    notifications.notify_import({"webhook_enabled": "true", "webhook_url": ""}, 1)
    await asyncio.sleep(0.01)


HELMARR = {"helmarr_enabled": "true", "helmarr_url": "https://notify.helmarr.test/device?source=sonarr"}


@respx.mock
async def test_helmarr_batches_a_series_into_one_tautulli_push(monkeypatch):
    monkeypatch.setattr(notifications, "HELMARR_QUIET_SECONDS", 0.02)
    route = respx.post("https://notify.helmarr.test/device").mock(return_value=Response(200))
    for number in (1, 2, 3):
        notifications.notify_helmarr(HELMARR, 7, "Berserk", "http://covers.test/7.jpg", f"Chapter {number}")
        await asyncio.sleep(0.005)
    notifications.notify_helmarr(HELMARR, 8, "Eleceed", "", "Chapter 12.5")
    await asyncio.sleep(0.1)

    assert {c.request.url.params["source"] for c in route.calls} == {"tautulli"}
    bodies = sorted((json.loads(c.request.content) for c in route.calls), key=lambda b: b["title"])
    assert bodies == [
        {"action": "created", "title": "Berserk - 3 chapters", "poster_url": "http://covers.test/7.jpg"},
        {"action": "created", "title": "Eleceed - Chapter 12.5", "poster_url": ""},
    ]
    assert notifications._helmarr_batches == {}


@respx.mock
async def test_helmarr_counts_every_file_of_a_torrent_import(monkeypatch):
    monkeypatch.setattr(notifications, "HELMARR_QUIET_SECONDS", 0.01)
    route = respx.post("https://notify.helmarr.test/device").mock(return_value=Response(200))
    notifications.notify_helmarr(HELMARR, 7, "Berserk", "", "1 chapter", 40)
    await asyncio.sleep(0.05)
    assert json.loads(route.calls.last.request.content)["title"] == "Berserk - 40 chapters"


async def test_helmarr_disabled_schedules_nothing():
    notifications.notify_helmarr({"helmarr_enabled": "false", "helmarr_url": "http://x"}, 1, "A", "", "Chapter 1")
    notifications.notify_helmarr({"helmarr_enabled": "true", "helmarr_url": ""}, 1, "A", "", "Chapter 1")
    assert notifications._helmarr_batches == {}
