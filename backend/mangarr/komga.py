"""Komga, the server that syncs the e-reader copies to a Kobo: its library is
scanned when copies change, and reading progress moves from chapter copies
into the volume they were merged into."""

import httpx

from . import USER_AGENT


class Komga:
    def __init__(self, url: str, api_key: str, client: httpx.AsyncClient | None = None):
        self._base = url.rstrip("/") + "/api/v1"
        self._headers = {"X-API-Key": api_key, "User-Agent": USER_AGENT}
        self._client = client or httpx.AsyncClient(timeout=30, trust_env=False)

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.request(method, self._base + path, headers=self._headers, **kwargs)
        resp.raise_for_status()
        return resp

    async def scan(self, library_id: str) -> None:
        await self._call("POST", f"/libraries/{library_id}/scan")

    async def series_books(self, library_id: str, name: str) -> dict[str, dict]:
        """The books of the library's series called `name` (its folder name
        or title), by book name."""
        found = await self._call("GET", "/series", params={"library_id": library_id, "search": name, "unpaged": "true"})
        for series in found.json().get("content") or []:
            if name in (series.get("name"), (series.get("metadata") or {}).get("title")):
                books = await self._call("GET", f"/series/{series['id']}/books", params={"unpaged": "true"})
                return {book["name"]: book for book in books.json().get("content") or []}
        return {}

    async def set_progress(self, book_id: str, page: int | None) -> None:
        """Mark a book read up to `page`, or finished when `page` is None."""
        body = {"completed": True} if page is None else {"page": page}
        await self._call("PATCH", f"/books/{book_id}/read-progress", json=body)


def from_settings(values: dict[str, str]) -> tuple[Komga, str] | None:
    """The configured Komga and the id of its library holding the copies."""
    if values.get("komga_url") and values.get("komga_api_key") and values.get("komga_library_id"):
        return Komga(values["komga_url"], values["komga_api_key"]), values["komga_library_id"]
    return None


def volume_progress(chapter_books: list[dict | None]) -> int | str | None:
    """Where a reader is in a volume made of these chapter books (in reading
    order): the last page read, "completed" when every chapter was finished,
    or None when none was started. A chapter Komga doesn't know counts as
    unread. Each chapter copy opens with a generated cover the volume
    doesn't repeat."""
    page = 0
    for book in chapter_books:
        progress = (book or {}).get("readProgress") or {}
        if not progress.get("completed"):
            return page + max((progress.get("page") or 0) - 1, 0) or None
        page += max((((book or {}).get("media") or {}).get("pagesCount") or 0) - 1, 0)
    return "completed" if chapter_books else None
