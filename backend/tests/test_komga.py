import json

import httpx
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr.komga import Komga, volume_progress
from mangarr.library import ereader
from mangarr.models import Base, Series


def book(pages, page=None, completed=False):
    progress = {"page": page or pages, "completed": completed} if page or completed else None
    return {"media": {"pagesCount": pages}, "readProgress": progress}


def test_volume_progress_follows_the_chapters_in_order():
    assert volume_progress([book(20), book(18)]) is None
    assert volume_progress([book(20, completed=True), book(18, page=5), book(22)]) == 23
    assert volume_progress([book(20, completed=True), book(18, page=1)]) == 19
    assert volume_progress([book(20, completed=True), None]) == 19
    assert volume_progress([book(20, page=1)]) is None
    assert volume_progress([book(20, completed=True), book(18, completed=True)]) == "completed"


@respx.mock
async def test_client_finds_a_series_by_folder_name_and_sets_progress():
    base = "http://komga:25600/api/v1"
    respx.get(f"{base}/series").respond(json={"content": [
        {"id": "s2", "name": "Berserk Gaiden", "metadata": {"title": "Berserk Gaiden"}},
        {"id": "s1", "name": "Berserk", "metadata": {"title": "Berserk"}},
    ]})
    respx.get(f"{base}/series/s1/books").respond(json={"content": [{"id": "b1", "name": "Berserk - Vol. 05"}]})
    progress = respx.patch(f"{base}/books/b1/read-progress").respond(204)
    komga = Komga("http://komga:25600/", "key", httpx.AsyncClient())
    assert list(await komga.series_books("lib", "Berserk")) == ["Berserk - Vol. 05"]
    await komga.set_progress("b1", 25)
    assert json.loads(progress.calls[0].request.content) == {"page": 25}
    assert progress.calls[0].request.headers["X-API-Key"] == "key"


async def test_waiting_progress_lands_once_komga_lists_the_volume(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    waiting = {"5": 25, "6": "completed"}
    async with maker() as session:
        session.add(Series(id=1, title="A", pending_progress=json.dumps(waiting)))
        await session.commit()
    monkeypatch.setattr(ereader, "session_scope", maker)

    class FakeKomga:
        scans, progress = 0, []

        async def scan(self, library_id):
            self.scans += 1

        async def series_books(self, library_id, name):
            # volume 6 is listed but not analysed yet
            return {"A - Vol. 05": {"id": "b5", "media": {"pagesCount": 200}},
                    "A - Vol. 06": {"id": "b6", "media": {"pagesCount": 0}}}

        async def set_progress(self, book_id, page):
            self.progress.append((book_id, page))

        async def library_books(self, library_id):
            return []

    komga = FakeKomga()
    vol5 = tmp_path / "A - Vol. 05.cbz"
    by_volume = {5: vol5, 6: tmp_path / "A - Vol. 06.cbz"}
    await ereader.update_komga(komga, "lib", True, [(1, "A", waiting, by_volume)])
    assert komga.scans == 1 and komga.progress == [("b5", 26)]
    async with maker() as session:
        assert json.loads((await session.get(Series, 1)).pending_progress) == {"6": "completed"}
    await engine.dispose()


async def test_books_komga_numbered_by_name_are_refreshed():
    def book(id, name, number, status="READY", locked=False):
        return {"id": id, "name": name, "media": {"status": status},
                "metadata": {"numberSort": number, "numberSortLock": locked}}

    class FakeKomga:
        refreshed = []

        async def library_books(self, library_id):
            return [
                book("ok", "A - Ch. 0002.5", 2.5),
                book("wrong", "A - Ch. 0041-0050", 5),
                book("unread", "A - Vol. 06", 4, status="UNKNOWN"),
                book("locked", "A - Vol. 07", 1, locked=True),
                book("other", "Extras", 1),
            ]

        async def refresh_metadata(self, book_id):
            self.refreshed.append(book_id)

    komga = FakeKomga()
    await ereader.update_komga(komga, "lib", False, [])
    assert komga.refreshed == ["wrong"]
