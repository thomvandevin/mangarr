import base64
import json

import httpx
import pytest
import respx

from mangarr.download.client import torrent_client
from mangarr.download.qbittorrent import QbtClient, QbtError
from mangarr.download.transmission import SESSION_HEADER, TransmissionClient
from mangarr.settings_service import DEFAULTS, validate

BASE = "http://transmission:9091"
RPC = f"{BASE}/transmission/rpc"


def answering(arguments: dict | None = None, result: str = "success"):
    calls: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"result": result, "arguments": arguments or {}})

    return calls, answer


@respx.mock
async def test_session_id_handshake_and_basic_auth():
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get(SESSION_HEADER) != "abc":
            return httpx.Response(409, headers={SESSION_HEADER: "abc"})
        return httpx.Response(200, json={"result": "success", "arguments": {"version": "4.0.6"}})

    route = respx.post(RPC).mock(side_effect=answer)
    client = TransmissionClient(BASE, "user", "pw")
    assert await client.version() == "4.0.6"
    assert route.call_count == 2
    assert route.calls.last.request.headers["Authorization"] == "Basic " + base64.b64encode(b"user:pw").decode()
    await client.version()
    assert route.call_count == 3


@respx.mock
async def test_add_magnet_labels_the_category_and_sets_the_download_dir():
    calls, answer = answering()
    respx.post(RPC).mock(side_effect=answer)
    await TransmissionClient(BASE, "", "").add_magnet("magnet:?xt=urn:btih:abc", "mangarr", "/downloads/mangarr")
    assert calls == [{"method": "torrent-add", "arguments": {
        "labels": ["mangarr"], "filename": "magnet:?xt=urn:btih:abc", "download-dir": "/downloads/mangarr",
    }}]


@respx.mock
async def test_add_torrent_sends_the_file_as_metainfo():
    calls, answer = answering()
    respx.post(RPC).mock(side_effect=answer)
    await TransmissionClient(BASE, "", "").add_torrent(b"d4:infoe", "mangarr")
    assert calls[0]["arguments"] == {"labels": ["mangarr"], "metainfo": base64.b64encode(b"d4:infoe").decode()}


@pytest.mark.parametrize(("percent", "left", "status", "complete"), [
    (1, 0, 6, True),
    (1, 0, 0, True),
    (1, 0, 4, False),
    (0.5, 100, 4, False),
    (0, 0, 4, False),
])
@respx.mock
async def test_get_torrent_is_complete_only_when_seeding_side(percent, left, status, complete):
    _, answer = answering({"torrents": [{
        "hashString": "abc", "name": "Berserk v01", "percentDone": percent, "leftUntilDone": left,
        "status": status, "downloadDir": "/downloads/mangarr/", "labels": ["mangarr"],
    }]})
    respx.post(RPC).mock(side_effect=answer)
    torrent = await TransmissionClient(BASE, "", "").get_torrent("abc")
    assert torrent.is_complete is complete
    assert torrent.progress == percent
    assert torrent.content_path == "/downloads/mangarr/Berserk v01"
    assert torrent.save_path == "/downloads/mangarr/"
    assert torrent.category == "mangarr"


@pytest.mark.parametrize(("status", "is_finished", "seeding_done"), [
    (0, True, True),
    (0, False, False),
    (6, False, False),
])
@respx.mock
async def test_seeding_is_done_only_when_transmission_stopped_it_at_its_limits(status, is_finished, seeding_done):
    _, answer = answering({"torrents": [{
        "hashString": "abc", "name": "x", "percentDone": 1, "leftUntilDone": 0,
        "status": status, "isFinished": is_finished, "downloadDir": "/d", "labels": ["mangarr"],
    }]})
    respx.post(RPC).mock(side_effect=answer)
    torrent = await TransmissionClient(BASE, "", "").get_torrent("abc")
    assert torrent.is_complete and torrent.seeding_done is seeding_done


@respx.mock
async def test_list_torrents_keeps_the_category_label_only():
    _, answer = answering({"torrents": [
        {"hashString": "abc", "name": "ours", "labels": ["mangarr"]},
        {"hashString": "def", "name": "sonarr's", "labels": ["tv"]},
        {"hashString": "123", "name": "unlabelled"},
    ]})
    respx.post(RPC).mock(side_effect=answer)
    assert [t.name for t in await TransmissionClient(BASE, "", "").list_torrents("mangarr")] == ["ours"]


@respx.mock
async def test_get_torrent_unknown_hash_is_none():
    _, answer = answering({"torrents": []})
    respx.post(RPC).mock(side_effect=answer)
    assert await TransmissionClient(BASE, "", "").get_torrent("abc") is None


@respx.mock
async def test_torrent_files_skips_unwanted_files():
    _, answer = answering({"torrents": [{
        "files": [{"name": "Berserk/c001.cbz"}, {"name": "Berserk/extras.zip"}, {"name": "Berserk/c002.cbz"}],
        "fileStats": [{"wanted": True}, {"wanted": False}, {"wanted": True}],
    }]})
    respx.post(RPC).mock(side_effect=answer)
    assert await TransmissionClient(BASE, "", "").torrent_files("abc") == ["Berserk/c001.cbz", "Berserk/c002.cbz"]


@respx.mock
async def test_delete_torrents_removes_local_data():
    calls, answer = answering()
    respx.post(RPC).mock(side_effect=answer)
    client = TransmissionClient(BASE, "", "")
    await client.delete_torrents([])
    await client.delete_torrents(["abc", "def"])
    assert calls == [{"method": "torrent-remove", "arguments": {"ids": ["abc", "def"], "delete-local-data": True}}]


@respx.mock
async def test_errors_surface_as_qbt_errors():
    respx.post(RPC).mock(return_value=httpx.Response(401))
    with pytest.raises(QbtError, match="login failed"):
        await TransmissionClient(BASE, "user", "bad").version()
    _, answer = answering(result="invalid or corrupt torrent file")
    respx.post(RPC).mock(side_effect=answer)
    with pytest.raises(QbtError, match="rejected torrent-add: invalid or corrupt torrent file"):
        await TransmissionClient(BASE, "", "").add_magnet("magnet:?", "mangarr")
    respx.post(RPC).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(QbtError, match="Cannot reach Transmission"):
        await TransmissionClient(BASE, "", "").version()


def test_settings_select_the_torrent_client():
    values = {**DEFAULTS, "qbittorrent_url": BASE}
    assert isinstance(torrent_client(values), QbtClient)
    assert isinstance(torrent_client({**values, "torrent_client": "transmission"}), TransmissionClient)
    with pytest.raises(ValueError, match="torrent_client"):
        validate({**values, "torrent_client": "deluge"})
