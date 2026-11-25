from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.download.qbittorrent import QbtTorrent
from mangarr.jobs import tasks
from mangarr.models import Base, Download, DownloadKind, DownloadStatus


def torrent(info_hash: str, state: str) -> QbtTorrent:
    return QbtTorrent(hash=info_hash, name=info_hash, progress=1.0, state=state, content_path="", category="mangarr")


class FakeClient:
    torrents: list[QbtTorrent] = []
    deleted: list[str] = []

    def __init__(self, *args):
        pass

    async def list_torrents(self, category):
        assert category == "mangarr"
        return FakeClient.torrents

    async def delete_torrents(self, hashes, delete_files=True):
        assert delete_files
        FakeClient.deleted.extend(hashes)

    async def close(self):
        pass


@pytest.fixture
async def cleanup(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    async def run(downloads: dict[str, list[DownloadStatus]], torrents: list[QbtTorrent], enabled: str = "true"):
        async with maker() as session:
            await settings_service.set_many(session, {"qbittorrent_enabled": "true", "torrent_remove_completed": enabled})
            for info_hash, statuses in downloads.items():
                for status in statuses:
                    session.add(Download(kind=DownloadKind.TORRENT, status=status, torrent_hash=info_hash))
            await session.commit()
        FakeClient.torrents, FakeClient.deleted = torrents, []
        await tasks.remove_finished_torrents()
        return FakeClient.deleted

    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks, "QbtClient", FakeClient)
    yield run
    await engine.dispose()


async def test_removes_imported_torrents_that_stopped_seeding_and_failed_imports(cleanup):
    deleted = await cleanup(
        {
            "done-stopped": [DownloadStatus.DONE],
            "done-seeding": [DownloadStatus.DONE],
            "failed": [DownloadStatus.FAILED],
            "regrabbed": [DownloadStatus.FAILED, DownloadStatus.DOWNLOADING],
            "importing": [DownloadStatus.IMPORTING],
        },
        [
            torrent("done-stopped", "pausedUP"),
            torrent("done-seeding", "uploading"),
            torrent("failed", "uploading"),
            torrent("regrabbed", "pausedUP"),
            torrent("importing", "pausedUP"),
            torrent("not-mangarrs", "pausedUP"),
        ],
    )
    assert deleted == ["done-stopped", "failed"]


async def test_removes_nothing_unless_enabled(cleanup):
    assert await cleanup({"done": [DownloadStatus.DONE]}, [torrent("done", "pausedUP")], enabled="false") == []
