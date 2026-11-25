import zipfile
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mangarr import settings_service
from mangarr.download.qbittorrent import QbtError, QbtTorrent
from mangarr.download.sabnzbd import SabnzbdClient
from mangarr.jobs import tasks
from mangarr.models import Base, Chapter, Download, DownloadKind, DownloadStatus, RootFolder, Series
from mangarr.sources.base import TorrentIndexer, TorrentRelease
from mangarr.sources.torznab import TorznabIndexer
from mangarr.torrent_selection import select_best_torrent

BASE = "http://sabnzbd:8080"
API = f"{BASE}/api"
JOB = "138bc196-b2a2-4d3d-bff2-78b732118d1a"
NZO = "nzb:" + JOB
NZB = b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'
SAB = {"sabnzbd_enabled": "true", "sabnzbd_url": BASE, "sabnzbd_api_key": "key", "sabnzbd_category": "mangarr"}


def sab(answers: dict[str, dict]):
    """SABnzbd answering each API mode from `answers`, recording the requests."""
    calls: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.method == "POST":
            params = {"mode": "addfile", "body": request.content}
        calls.append(params)
        return httpx.Response(200, json=answers.get(params["mode"], {"status": True}))

    respx.route(url__startswith=API).mock(side_effect=answer)
    return calls


@respx.mock
async def test_add_nzb_uploads_the_file_under_the_category_and_returns_the_job_id():
    calls = sab({"addfile": {"status": True, "nzo_ids": [JOB]}})
    assert await SabnzbdClient(BASE, "key").add_nzb(NZB, "Berserk v20", "mangarr") == NZO
    body = calls[0]["body"]
    assert NZB in body and b'filename="Berserk v20.nzb"' in body
    for field in (b"mangarr", b"Berserk v20", b"key"):
        assert field in body


@respx.mock
async def test_get_torrent_reports_queue_progress():
    calls = sab({"queue": {"queue": {"slots": [{"nzo_id": JOB, "filename": "Berserk v20", "percentage": "40"}]}}})
    record = await SabnzbdClient(BASE, "key").get_torrent(NZO)
    assert (record.hash, record.progress, record.is_complete, record.error) == (NZO, 0.4, False, "")
    assert calls[0]["nzo_ids"] == JOB


@respx.mock
async def test_get_torrent_completes_with_the_storage_path():
    sab({"history": {"history": {"slots": [
        {"nzo_id": JOB, "name": "Berserk v20", "status": "Completed", "storage": "/data/downloads/complete/mangarr/Berserk v20"},
    ]}}})
    record = await SabnzbdClient(BASE, "key").get_torrent(NZO)
    assert record.is_complete and record.content_path == "/data/downloads/complete/mangarr/Berserk v20"
    assert record.save_path == ""


@respx.mock
async def test_get_torrent_carries_the_failure_and_misses_unknown_jobs():
    sab({"history": {"history": {"slots": [{"nzo_id": JOB, "status": "Failed", "fail_message": "Aborted, cannot be completed"}]}}})
    record = await SabnzbdClient(BASE, "key").get_torrent(NZO)
    assert not record.is_complete and record.error == "Aborted, cannot be completed"
    assert await SabnzbdClient(BASE, "key").get_torrent("nzb:other") is None


@respx.mock
async def test_delete_clears_queue_and_history():
    calls = sab({"queue": {"status": False, "nzo_ids": []}})
    await SabnzbdClient(BASE, "key").delete_torrents([NZO])
    assert [(c["mode"], c["name"], c["value"], c["del_files"]) for c in calls] == [
        ("queue", "delete", JOB, "1"), ("history", "delete", JOB, "1"),
    ]


@respx.mock
async def test_errors_do_not_leak_the_api_key():
    respx.route(url__startswith=API).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(QbtError) as exc:
        await SabnzbdClient(BASE, "s3cret").version()
    assert "s3cret" not in str(exc.value)
    respx.route(url__startswith=API).mock(return_value=httpx.Response(200, json={"status": False, "error": "API Key Incorrect"}))
    with pytest.raises(QbtError, match="API Key Incorrect"):
        await SabnzbdClient(BASE, "bad").version()


FEED = b"""<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
<item><title>Berserk v20 (2007) (Digital) (danke-Empire)</title>
  <enclosure url="http://prowlarr:9696/20/download?apikey=k&amp;file=Berserk+v20" length="542000000" type="application/x-nzb"/>
  <newznab:attr name="size" value="542000000"/></item>
<item><title>Berserk v20 torrent</title>
  <enclosure url="http://prowlarr:9696/1/download?file=x" length="10" type="application/x-bittorrent"/></item>
</channel></rss>"""


async def test_torznab_marks_nzb_enclosures_as_usenet_and_never_fetches_them():
    indexer = TorznabIndexer({"name": "Prowlarr", "url": "http://prowlarr:9696/20/api", "api_key": "k"})
    usenet, torrent = indexer.parse(FEED)
    assert (usenet.usenet, torrent.usenet) == (True, False)
    assert usenet.magnet == "http://prowlarr:9696/20/download?apikey=k&file=Berserk+v20"
    assert await indexer.get_torrent_metadata(usenet) == b""


@respx.mock
async def test_submit_routes_an_nzb_url_to_sabnzbd():
    respx.get("http://prowlarr:9696/20/download").mock(return_value=httpx.Response(200, content=NZB))
    calls = sab({"addfile": {"status": True, "nzo_ids": [JOB]}})
    url = "http://prowlarr:9696/20/download?apikey=k&file=Berserk+v20+%282007%29"
    assert await tasks.submit_torrent(url, SAB) == NZO
    assert calls[0] == {"mode": "set_config", "apikey": "key", "output": "json", "section": "categories",
                        "keyword": "mangarr", "dir": "mangarr"}
    assert b'filename="Berserk v20 (2007).nzb"' in calls[1]["body"]
    with pytest.raises(ValueError, match="SABnzbd is not enabled"):
        await tasks.submit_torrent(url, {**SAB, "sabnzbd_enabled": "false"})


class OneRelease(TorrentIndexer):
    name = "fake"

    def __init__(self, release):
        self.release = release

    async def search(self, query: str):
        return [self.release]


async def test_selection_takes_a_seederless_usenet_release_only_with_sabnzbd_enabled():
    release = TorrentRelease(source_name="fake", title="Series c001-c003", magnet="http://nzb", size_bytes=100, usenet=True)
    series = Series(title="Series", sort_title="series")
    wanted = [Chapter(number=float(n), downloaded=False) for n in (1, 2, 3)]

    async def select(values):
        return await select_best_torrent(series, wanted, [OneRelease(release)], max_size_bytes=1000, min_seeders=1, values=values)

    assert (await select(SAB)).coverage == {1, 2, 3}
    assert await select({}) is None


class FakeSab:
    record: QbtTorrent
    deleted: list[str] = []

    async def get_torrent(self, nzo_id):
        return FakeSab.record

    async def delete_torrents(self, ids, delete_files=True):
        FakeSab.deleted.extend(ids)

    async def close(self):
        pass


@pytest.fixture
async def usenet_download(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    payload = tmp_path / "downloads" / "Test Series c001"
    payload.mkdir(parents=True)
    with zipfile.ZipFile(payload / "Test Series - c001.cbz", "w") as zf:
        zf.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (tmp_path / "library").mkdir()
    async with maker() as session:
        await settings_service.set_many(session, SAB)
        series = Series(title="Test Series", sort_title="test series",
                        root_folder=RootFolder(path=str(tmp_path / "library")), folder_name="Test Series")
        series.chapters = [Chapter(number=1.0, downloaded=False)]
        session.add(series)
        await session.commit()
        dl = Download(series_id=series.id, kind=DownloadKind.TORRENT, status=DownloadStatus.DOWNLOADING,
                      title="Test Series c001", source_name="Torznab / Prowlarr", payload="http://nzb", torrent_hash=NZO)
        session.add(dl)
        await session.commit()

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    FakeSab.deleted = []
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks, "download_client", lambda *args: FakeSab())
    monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
    monkeypatch.setattr(tasks.notifications, "notify_helmarr", lambda *a: None)
    monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)

    async def status():
        async with maker() as session:
            row = await session.get(Download, dl.id)
            return row.status, row.error

    yield payload, status
    tasks._torrent_missing_counts.clear()
    tasks._import_path_missing_counts.clear()
    await engine.dispose()


async def test_sync_imports_a_finished_usenet_download_and_clears_it_from_sabnzbd(usenet_download, tmp_path):
    payload, status = usenet_download
    FakeSab.record = QbtTorrent(hash=NZO, name="x", progress=1.0, state="uploading", content_path=str(payload), category="mangarr")
    await tasks.sync_qbittorrent()
    assert await status() == (DownloadStatus.DONE, "")
    assert list((tmp_path / "library" / "Test Series").glob("*.cbz"))
    assert FakeSab.deleted == [NZO]
    assert not payload.exists()


async def test_sync_removes_a_single_file_usenet_payload_and_its_job_folder(usenet_download, tmp_path):
    payload, status = usenet_download
    FakeSab.record = QbtTorrent(hash=NZO, name="x", progress=1.0, state="uploading",
                                content_path=str(payload / "Test Series - c001.cbz"), category="mangarr")
    await tasks.sync_qbittorrent()
    assert await status() == (DownloadStatus.DONE, "")
    assert list((tmp_path / "library" / "Test Series").glob("*.cbz"))
    assert not payload.exists() and payload.parent.exists()


async def test_sync_fails_a_download_sabnzbd_gave_up(usenet_download):
    _, status = usenet_download
    FakeSab.record = QbtTorrent(hash=NZO, name="x", progress=1.0, state="downloading", content_path="", category="",
                                error="Aborted, cannot be completed")
    await tasks.sync_qbittorrent()
    assert await status() == (DownloadStatus.FAILED, "Aborted, cannot be completed")
    assert FakeSab.deleted == []
