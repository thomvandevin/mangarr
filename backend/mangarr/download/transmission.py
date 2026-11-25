"""Transmission RPC client with the surface of QbtClient, so the torrent
pipeline can drive either download client."""

import base64

import httpx

from .. import USER_AGENT
from .qbittorrent import QbtError, QbtTorrent

SESSION_HEADER = "X-Transmission-Session-Id"
TORRENT_FIELDS = ["hashString", "name", "percentDone", "leftUntilDone", "status", "downloadDir", "labels"]
# stopped, queued to seed, seeding
SEEDING_SIDE_STATUSES = (0, 5, 6)


class TransmissionClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.rpc_url = f"{base_url.rstrip('/')}/transmission/rpc"
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            auth=(username, password) if username else None,
            timeout=30,
        )
        self._session_id = ""

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, body: dict) -> httpx.Response:
        try:
            return await self._client.post(self.rpc_url, json=body, headers={SESSION_HEADER: self._session_id})
        except httpx.HTTPError:
            raise QbtError("Cannot reach Transmission") from None

    async def _call(self, method: str, **arguments) -> dict:
        body = {"method": method, "arguments": arguments}
        resp = await self._send(body)
        # Transmission answers 409 with the session id every later request must carry.
        if resp.status_code == 409:
            self._session_id = resp.headers.get(SESSION_HEADER, "")
            resp = await self._send(body)
        if resp.status_code == 401:
            raise QbtError("Transmission login failed: HTTP 401")
        if not resp.is_success:
            raise QbtError(f"Transmission request failed: HTTP {resp.status_code}")
        answer = resp.json()
        if answer.get("result") != "success":
            raise QbtError(f"Transmission rejected {method}: {answer.get('result')}")
        return answer.get("arguments") or {}

    async def version(self) -> str:
        return (await self._call("session-get", fields=["version"])).get("version", "")

    async def default_save_path(self) -> str:
        try:
            return (await self._call("session-get", fields=["download-dir"])).get("download-dir", "")
        except QbtError:
            return ""

    async def ensure_category(self, name: str, save_path: str | None = None) -> None:
        """Transmission has no categories to create: a torrent carries its
        category as a label and its own download directory."""

    async def _add(self, category: str, save_path: str | None, **source) -> None:
        arguments = {"labels": [category], **source}
        if save_path:
            arguments["download-dir"] = save_path
        await self._call("torrent-add", **arguments)

    async def add_magnet(self, magnet: str, category: str, save_path: str | None = None) -> None:
        await self._add(category, save_path, filename=magnet)

    async def add_torrent(self, content: bytes, category: str, save_path: str | None = None) -> None:
        await self._add(category, save_path, metainfo=base64.b64encode(content).decode())

    async def delete_torrents(self, hashes: list[str], delete_files: bool = True) -> None:
        if not hashes:
            return
        await self._call("torrent-remove", ids=hashes, **{"delete-local-data": delete_files})

    async def get_torrent(self, torrent_hash: str) -> QbtTorrent | None:
        torrents = (await self._call("torrent-get", ids=[torrent_hash], fields=TORRENT_FIELDS)).get("torrents") or []
        if not torrents:
            return None
        t = torrents[0]
        progress = float(t.get("percentDone", 0))
        complete = progress >= 1 and t.get("leftUntilDone") == 0 and t.get("status") in SEEDING_SIDE_STATUSES
        download_dir = t.get("downloadDir", "")
        return QbtTorrent(
            hash=t.get("hashString", ""),
            name=t.get("name", ""),
            progress=progress,
            # QbtTorrent.is_complete reads qBittorrent's state names
            state="uploading" if complete else "downloading",
            content_path=f"{download_dir.rstrip('/')}/{t.get('name', '')}" if download_dir else "",
            category=next(iter(t.get("labels") or []), ""),
            save_path=download_dir,
        )

    async def torrent_files(self, torrent_hash: str) -> list[str]:
        """The torrent's wanted files, relative to its download directory."""
        answer = await self._call("torrent-get", ids=[torrent_hash], fields=["files", "fileStats"])
        torrents = answer.get("torrents") or []
        if not torrents:
            return []
        files, stats = torrents[0].get("files") or [], torrents[0].get("fileStats") or []
        return [
            f["name"] for i, f in enumerate(files)
            if f.get("name") and (i >= len(stats) or stats[i].get("wanted", True))
        ]


async def test_connection(base_url: str, username: str, password: str) -> str:
    """Returns Transmission's version or raises QbtError."""
    client = TransmissionClient(base_url, username, password)
    try:
        return await client.version()
    finally:
        await client.close()
