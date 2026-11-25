"""SABnzbd API client for usenet releases. Downloads are tracked through the
torrent pipeline, so lookups answer with QbtTorrent records."""

import httpx

from .. import USER_AGENT
from .qbittorrent import QbtError, QbtTorrent

# Marks a download id as a SABnzbd job. SABnzbd's own ids have no fixed
# shape (plain UUIDs since 4.x), so they can't be told from info hashes.
NZB_ID_PREFIX = "nzb:"


def is_nzb(content: bytes) -> bool:
    return b"<nzb" in content[:4096]


class SabnzbdClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.api_url = f"{base_url.rstrip('/')}/api"
        self.api_key = api_key
        self._client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, mode: str, files: dict | None = None, check: bool = True, **params) -> dict:
        params = {"mode": mode, "apikey": self.api_key, "output": "json", **params}
        try:
            if files:
                resp = await self._client.post(self.api_url, data=params, files=files)
            else:
                resp = await self._client.get(self.api_url, params=params)
        except httpx.HTTPError:
            # Transport exceptions carry the URL, which holds the API key.
            raise QbtError("Cannot reach SABnzbd") from None
        if not resp.is_success:
            raise QbtError(f"SABnzbd request failed: HTTP {resp.status_code}")
        try:
            answer = resp.json()
        except ValueError:
            raise QbtError("SABnzbd sent an unreadable answer") from None
        if check and answer.get("status") is False:
            raise QbtError(f"SABnzbd rejected {mode}: {answer.get('error', 'unknown error')}")
        return answer

    async def version(self) -> str:
        # mode=version answers without a key, so the queue call proves the key works
        await self._call("queue", limit=0)
        return (await self._call("version")).get("version", "")

    async def ensure_category(self, name: str) -> None:
        """Create the category (idempotent) so its downloads land in their own folder."""
        await self._call("set_config", section="categories", keyword=name, dir=name)

    async def add_nzb(self, content: bytes, name: str, category: str) -> str:
        answer = await self._call(
            "addfile", files={"name": (f"{name}.nzb", content, "application/x-nzb")},
            cat=category, nzbname=name,
        )
        ids = answer.get("nzo_ids") or []
        if not ids:
            raise QbtError("SABnzbd did not queue the NZB")
        return NZB_ID_PREFIX + ids[0]

    async def get_torrent(self, download_id: str) -> QbtTorrent | None:
        nzo_id = download_id.removeprefix(NZB_ID_PREFIX)
        queue = (await self._call("queue", nzo_ids=nzo_id)).get("queue") or {}
        for slot in queue.get("slots") or []:
            if slot.get("nzo_id") == nzo_id:
                return QbtTorrent(
                    hash=download_id, name=slot.get("filename", ""), state="downloading",
                    progress=float(slot.get("percentage") or 0) / 100, content_path="", category=slot.get("cat", ""),
                )
        history = (await self._call("history", nzo_ids=nzo_id)).get("history") or {}
        for slot in history.get("slots") or []:
            if slot.get("nzo_id") != nzo_id:
                continue
            status = slot.get("status", "")
            record = QbtTorrent(
                hash=download_id, name=slot.get("name", ""), state="downloading", progress=1.0,
                content_path="", category=slot.get("category", ""),
            )
            if status == "Completed":
                # QbtTorrent.is_complete reads qBittorrent's state names
                record.state, record.content_path = "uploading", slot.get("storage", "")
            elif status == "Failed":
                record.error = slot.get("fail_message") or "SABnzbd could not complete the download"
            return record
        return None

    async def delete_torrents(self, download_ids: list[str], delete_files: bool = True) -> None:
        if not download_ids:
            return
        nzo_ids = ",".join(i.removeprefix(NZB_ID_PREFIX) for i in download_ids)
        # a job is in the queue or in the history, and SABnzbd answers
        # status false for the place it is not in
        for mode in ("queue", "history"):
            await self._call(mode, check=False, name="delete", value=nzo_ids, del_files=int(delete_files))


async def test_connection(base_url: str, api_key: str) -> str:
    """Returns SABnzbd's version or raises QbtError."""
    client = SabnzbdClient(base_url, api_key)
    try:
        return await client.version()
    finally:
        await client.close()
