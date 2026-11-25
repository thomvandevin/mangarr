import pytest

from mangarr.models import Chapter, Series
from mangarr.sources.base import TorrentIndexer, TorrentRelease
from mangarr.torrent_selection import (
    BencodeError,
    bdecode,
    coverage_from_text,
    release_matches_series,
    select_best_torrent,
    torrent_coverage,
    torrent_paths,
    torrent_volume_sizes,
)


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(
            bencode(key) + bencode(value[key]) for key in sorted(value)
        ) + b"e"
    raise TypeError(type(value))


def torrent_with(*names: str) -> bytes:
    return bencode(
        {
            b"announce": b"https://tracker",
            b"info": {
                b"name": b"release",
                b"files": [
                    {b"length": 123, b"path": [part.encode() for part in name.split("/")]}
                    for name in names
                ],
            },
        }
    )


def chapters():
    return [
        Chapter(number=1, volume=1),
        Chapter(number=2, volume=1),
        Chapter(number=2.5, volume=1),
        Chapter(number=3, volume=2),
        Chapter(number=4, volume=2),
    ]


def test_torrent_paths_reads_multifile_info():
    metadata = torrent_with("Series/Series c002.5.cbz", "Series/cover.jpg")
    assert torrent_paths(metadata) == ["Series/Series c002.5.cbz", "Series/cover.jpg"]


def test_bdecode_rejects_noncanonical_and_excessively_nested_data():
    for invalid in (b"i01e", b"i-0e", b"01:a", b"d1:bi1e1:ai2ee"):
        try:
            bdecode(invalid)
        except BencodeError:
            pass
        else:
            raise AssertionError(f"accepted non-canonical bencode: {invalid!r}")

    nested = b"l" * 102 + b"e" * 102
    try:
        bdecode(nested)
    except BencodeError:
        pass
    else:
        raise AssertionError("accepted excessively nested bencode")


def test_torrent_file_list_covers_chapters_and_whole_volumes():
    metadata = torrent_with("Series/Series v01.cbz", "Series/Series c003.cbz")
    assert torrent_coverage(metadata, "Series release", chapters()) == {1, 2, 2.5, 3}


def test_release_title_ranges_are_a_fallback():
    assert coverage_from_text("Series c001-c003", chapters()) == {1, 2, 2.5, 3}
    assert coverage_from_text("Series v01-v02 (Digital)", chapters()) == {1, 2, 2.5, 3, 4}


def test_file_list_overrides_broad_title_range():
    metadata = torrent_with(
        "Series/Series c001.cbz", "Series/Series c002.cbz", "Series/Series c003.cbz"
    )
    assert torrent_coverage(metadata, "Series c001-c003", chapters()) == {1, 2, 3}


def test_bare_numeric_archive_uses_release_title_fallback():
    metadata = torrent_with("Series/001.cbz")
    series = Series(title="Series", sort_title="series")
    assert torrent_coverage(
        metadata, "Series c001-c003", chapters(), series
    ) == {1, 2, 2.5, 3}


def test_short_series_title_matches_as_a_whole_token():
    series = Series(title="GTO", sort_title="gto")
    assert release_matches_series("[Group] GTO v01-v25", series)
    assert not release_matches_series("[Group] GTOther v01", series)


@pytest.mark.parametrize(("release", "matches"), [
    ("Psyren v01-16 (2011-2014) (Digital) (danke-Empire)", True),
    ("[Group] (2011) Psyren - c145", True),
    ("Psyren (Digital) (danke-Empire)", True),
    ("Psyren", True),
    ("Saiko no Sainou / Psyren 001-145", True),
    ("Psyren.v05.2012.Digital.danke-Empire", True),
    ("The Weakest Tamer Began a Journey to Pick Up Trash v01-13 [Audiobook] [Seven Seas Siren] [Stick & Oak]", False),
    ("Psyren Another Call v01", False),
    ("Dark.Horse-Psyrens.Unbound.Vol.01", False),
])
def test_release_title_has_to_stand_where_a_title_stands(release, matches):
    series = Series(title="PSYЯEN", sort_title="psyren", alt_titles="Siren\nPsyren")
    assert release_matches_series(release, series) is matches


async def test_a_release_already_imported_for_the_series_is_not_selected_again():
    release = TorrentRelease(source_name="fake", title="Series c001-c003", magnet="magnet:again", size_bytes=100, seeders=5)
    indexer = FakeIndexer([release], {release.magnet: b""})

    async def select(blocked_titles):
        return await select_best_torrent(
            Series(title="Series", sort_title="series"), chapters(), [indexer],
            max_size_bytes=1000, min_seeders=1, blocked_titles=blocked_titles,
        )

    assert (await select(set())).release is release
    assert await select({"Series c001-c003"}) is None


class FakeIndexer(TorrentIndexer):
    name = "fake"

    def __init__(self, releases, metadata):
        self.releases = releases
        self.metadata = metadata

    async def search(self, query: str):
        return self.releases if query == "Series" else []

    async def get_torrent_metadata(self, release: TorrentRelease) -> bytes:
        return self.metadata[release.magnet]


async def test_selects_most_coverage_then_specials():
    smaller = TorrentRelease(
        source_name="fake",
        title="Series c001-c003",
        magnet="magnet:small",
        torrent_url="https://small.torrent",
        size_bytes=100,
        seeders=20,
    )
    better = TorrentRelease(
        source_name="fake",
        title="Series volume pack",
        magnet="magnet:better",
        torrent_url="https://better.torrent",
        size_bytes=200,
        seeders=2,
    )
    indexer = FakeIndexer(
        [smaller, better],
        {
            smaller.magnet: torrent_with("Series c001.cbz", "Series c002.cbz", "Series c003.cbz"),
            better.magnet: torrent_with("Series v01.cbz", "Series v02.cbz"),
        },
    )
    series = Series(title="Series", sort_title="series")
    tracked = chapters()
    for chapter in tracked:
        chapter.monitored = True
        chapter.downloaded = False

    selected = await select_best_torrent(
        series, tracked, [indexer], max_size_bytes=1000, min_seeders=1
    )

    assert selected is not None
    assert selected.release is better
    assert selected.coverage == {1, 2, 2.5, 3, 4}


async def test_selects_from_title_when_metadata_is_unavailable():
    release = TorrentRelease(
        source_name="fake",
        title="Series c001-c003",
        magnet="magnet:title-only",
        size_bytes=100,
        seeders=5,
    )
    indexer = FakeIndexer([release], {release.magnet: b""})

    selected = await select_best_torrent(
        Series(title="Series", sort_title="series"),
        chapters(),
        [indexer],
        max_size_bytes=1000,
        min_seeders=1,
    )

    assert selected is not None
    assert selected.release is release
    assert selected.coverage == {1, 2, 2.5, 3}


async def test_volume_pack_without_volume_map_does_not_claim_exact_coverage():
    release = TorrentRelease(
        source_name="fake",
        title="Series v01-v02",
        magnet="magnet:volumes",
        size_bytes=100,
        seeders=5,
    )
    indexer = FakeIndexer(
        [release],
        {release.magnet: torrent_with("Series v01.cbz", "Series v02.cbz")},
    )
    unmapped = [Chapter(number=1), Chapter(number=2), Chapter(number=2.5)]

    selected = await select_best_torrent(
        Series(title="Series", sort_title="series"),
        unmapped,
        [indexer],
        max_size_bytes=1000,
        min_seeders=1,
    )

    # Without a chapter-to-volume map there is no safe exact exclusion set.
    # Guessing here would suppress direct grabs for chapters the pack may omit.
    assert selected is None


def _volume_pack():
    release = TorrentRelease(source_name="fake", title="Series v01-v02", magnet="magnet:pack", size_bytes=100, seeders=5)
    return release, torrent_with("Series v01.cbz", "Series v02.cbz")


async def _select(release, metadata, library_volumes):
    volumes = [Chapter(number=1, volume=1), Chapter(number=2, volume=2)]
    return await select_best_torrent(
        Series(title="Series", sort_title="series"), volumes, [FakeIndexer([release], {release.magnet: metadata})],
        max_size_bytes=1000, min_seeders=1, library_volumes=library_volumes,
    )


async def test_pack_with_a_volume_the_library_holds_as_other_bytes_is_skipped():
    release, metadata = _volume_pack()
    assert torrent_volume_sizes(metadata) == {1: 123, 2: 123}
    assert (await _select(release, metadata, {})).release is release
    assert (await _select(release, metadata, {1: 123})).release is release
    assert await _select(release, metadata, {1: 999}) is None


async def test_release_without_a_file_list_is_skipped_when_it_shares_a_volume_with_the_library():
    release, _ = _volume_pack()
    assert (await _select(release, b"", {3: 5})).release is release
    assert await _select(release, b"", {2: 5}) is None
