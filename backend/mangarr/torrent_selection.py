"""Inspect torrent metadata and choose the release with the best coverage."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .download.client import usenet_enabled
from .download.torrent import magnet_btih_hex, torrent_info_hash
from .filename import parse_filename
from .models import Chapter, Series
from .sources.base import TorrentIndexer, TorrentRelease
from .titles import metadata_title_queries
from .util import (
    is_special_chapter,
    normalize_title,
    safe_error_reason,
)

log = logging.getLogger(__name__)

INSPECTION_LIMIT = 20
MAX_BENCODE_DEPTH = 100

_ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z", ".pdf"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
_CANONICAL_INTEGER = re.compile(rb"(?:0|-[1-9]\d*|[1-9]\d*)\Z")
_CANONICAL_LENGTH = re.compile(rb"(?:0|[1-9]\d*)\Z")


class BencodeError(ValueError):
    pass


def bdecode(data: bytes):
    """Minimal, strict bencode decoder sufficient for torrent file lists."""

    def parse(offset: int, depth: int = 0):
        if depth > MAX_BENCODE_DEPTH:
            raise BencodeError("bencode nesting exceeds safety limit")
        if offset >= len(data):
            raise BencodeError("unexpected end of bencode data")
        token = data[offset : offset + 1]
        if token == b"i":
            end = data.find(b"e", offset + 1)
            if end < 0:
                raise BencodeError("unterminated integer")
            raw = data[offset + 1 : end]
            if not _CANONICAL_INTEGER.fullmatch(raw):
                raise BencodeError("non-canonical integer")
            return int(raw), end + 1
        if token in (b"l", b"d"):
            collection = [] if token == b"l" else {}
            cursor = offset + 1
            previous_key: bytes | None = None
            while cursor < len(data) and data[cursor : cursor + 1] != b"e":
                key, cursor = parse(cursor, depth + 1)
                if token == b"l":
                    collection.append(key)
                    continue
                if not isinstance(key, bytes):
                    raise BencodeError("dictionary key is not bytes")
                if previous_key is not None and key <= previous_key:
                    raise BencodeError("dictionary keys are not strictly sorted")
                previous_key = key
                value, cursor = parse(cursor, depth + 1)
                collection[key] = value
            if cursor >= len(data):
                raise BencodeError("unterminated collection")
            return collection, cursor + 1
        colon = data.find(b":", offset)
        if colon < 0:
            raise BencodeError("invalid byte string")
        raw_length = data[offset:colon]
        if not _CANONICAL_LENGTH.fullmatch(raw_length):
            raise BencodeError("non-canonical byte string length")
        length = int(raw_length)
        start, end = colon + 1, colon + 1 + length
        if end > len(data):
            raise BencodeError("byte string exceeds input")
        return data[start:end], end

    value, end = parse(0)
    if end != len(data):
        raise BencodeError("trailing bencode data")
    return value


def torrent_paths(metadata: bytes) -> list[str]:
    decoded = bdecode(metadata)
    if not isinstance(decoded, dict) or not isinstance(decoded.get(b"info"), dict):
        raise BencodeError("torrent has no info dictionary")
    info = decoded[b"info"]

    def text(value) -> str:
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""

    files = info.get(b"files")
    if isinstance(files, list):
        paths = []
        for item in files:
            components = item.get(b"path") if isinstance(item, dict) else None
            if isinstance(components, list):
                value = "/".join(filter(None, (text(part) for part in components)))
                if value:
                    paths.append(value)
        return paths
    name = text(info.get(b"name"))
    return [name] if name else []


def coverage_from_text(
    text: str,
    chapters: list[Chapter],
    *,
    allow_bare_chapter: bool = True,
) -> set[float]:
    parsed = parse_filename(text)
    return {
        chapter.number for chapter in chapters
        if parsed.covers(chapter.number, chapter.volume, allow_bare=allow_bare_chapter)
    }


def torrent_coverage(
    metadata: bytes,
    release_title: str,
    chapters: list[Chapter],
    series: Series | None = None,
) -> set[float]:
    """Chapter numbers represented by files, falling back to the release title."""
    paths = torrent_paths(metadata)
    covered: set[float] = set()
    image_parents: set[str] = set()
    for raw in paths:
        item = PurePosixPath(raw)
        suffix = item.suffix.lower()
        if suffix in _ARCHIVE_EXTS:
            covered.update(
                coverage_from_text(
                    item.stem,
                    chapters,
                    allow_bare_chapter=(
                        series is not None and release_matches_series(item.stem, series)
                    ),
                )
            )
        elif suffix in _IMAGE_EXTS:
            image_parents.add(str(item.parent))
    for parent in image_parents:
        name = PurePosixPath(parent).name
        covered.update(
            coverage_from_text(
                name,
                chapters,
                allow_bare_chapter=(
                    series is not None and release_matches_series(name, series)
                ),
            )
        )
    # A broad title such as "c001-c100" may omit decimal extras in the actual
    # file list. Once parseable files are available, treat them as authoritative.
    return covered or coverage_from_text(release_title, chapters)


_LEADING_TAG = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\))")
_TAG_OPEN = "\x00"
# what may follow the title: a tag, a number, or a volume or chapter marker
_AFTER_TITLE = re.compile(rf"{_TAG_OPEN}|\d|(v|c|ch) ?\d|(vol|volume|volumes|chapter|chapters)\b")


def _title_segments(release_title: str) -> list[str]:
    """The places a release name can start a title: its beginning, past any
    leading group tags, and after a "/" or "|" between two titles. Each is
    normalized like normalize_title, except that an opening bracket survives
    as _TAG_OPEN."""
    segments = []
    for part in re.split(r"\s[/|]\s", release_title):
        while match := _LEADING_TAG.match(part):
            part = part[match.end():]
        part = re.sub(r"[(\[{]", f" {_TAG_OPEN} ", part.lower().replace("×", "x"))
        part = re.sub(rf"[^a-z0-9{_TAG_OPEN}]+", " ", part).strip()
        if part:
            segments.append(part)
    return segments


def release_matches_series(release_title: str, series: Series) -> bool:
    """Whether a release is named after the series. The title has to stand
    where a title stands and be followed by a number or a tag, so a title
    inside another word, in a group name ("Siren" in "[Seven Seas Siren]")
    or of a spin-off ("Series Kitchen v04") does not match."""
    segments = _title_segments(release_title)
    aliases = metadata_title_queries(series)
    if getattr(series, "search_title", ""):
        aliases.insert(0, series.search_title)
    for alias in map(normalize_title, aliases):
        if len(alias.replace(" ", "")) < 3:
            continue
        for segment in segments:
            if segment == alias:
                return True
            if segment.startswith(alias + " ") and _AFTER_TITLE.match(segment[len(alias) + 1:]):
                return True
    return False


@dataclass
class TorrentSelection:
    release: TorrentRelease
    coverage: set[float]


async def select_best_torrent(
    series: Series,
    chapters: list[Chapter],
    indexers: list[TorrentIndexer],
    *,
    max_size_bytes: int,
    min_seeders: int,
    values: dict[str, str] | None = None,
    blocked_payloads: set[tuple[str, str]] | None = None,
    blocked_hashes: set[str] | None = None,
    blocked_titles: set[str] | None = None,
) -> TorrentSelection | None:
    # This selector is used by the explicit add-time "Search now" flow, which
    # intentionally searches even when the new series itself is unmonitored.
    chapters = [c for c in chapters if not c.excluded]
    wanted = {c.number for c in chapters if not c.downloaded}
    if not wanted or not indexers:
        return None
    from .sources.queries import search_queries

    queries = search_queries(series, [c for c in chapters if c.number in wanted], values or {})
    blocked_hashes = {info_hash.lower() for info_hash in (blocked_hashes or set())}
    candidates: list[tuple[TorrentIndexer, TorrentRelease]] = []
    seen: set[str] = set()
    for indexer in indexers:
        for query in queries:
            try:
                releases = await indexer.search(query)
            except Exception as exc:
                # logged so a blocked indexer is told apart from "no results"
                log.warning("indexer %s search failed: %s", indexer.name, safe_error_reason(exc))
                continue
            for release in releases:
                key = release.magnet
                if (
                    not key
                    or key in seen
                    or (release.source_name, key) in (blocked_payloads or set())
                    or (key.startswith("magnet:") and magnet_btih_hex(key) in blocked_hashes)
                    or release.title in (blocked_titles or set())
                    or (release.usenet and not usenet_enabled(values or {}))
                    or (not release.usenet and release.seeders < min_seeders)
                    or release.size_bytes <= 0
                    or release.size_bytes > max_size_bytes
                    or not release_matches_series(release.title, series)
                ):
                    continue
                seen.add(key)
                candidates.append((indexer, release))
            if len(candidates) >= INSPECTION_LIMIT:
                # A productive title query already supplied enough
                # releases to inspect; avoid spending rate-limit slots on up
                # to five redundant alternate-title searches.
                break
    # Seeder order puts viable metadata first and bounds work against broad
    # title queries without making popularity outrank actual file coverage.
    candidates.sort(key=lambda item: item[1].seeders, reverse=True)
    candidates = candidates[:INSPECTION_LIMIT]

    async def inspect(item: tuple[TorrentIndexer, TorrentRelease]):
        indexer, release = item
        try:
            metadata = await indexer.get_torrent_metadata(release)
            # Signed download URLs and tracker labels can change between feed
            # requests. The torrent's own identity must keep its cooldown.
            info_hash = torrent_info_hash(metadata) if metadata else release.info_hash.lower()
            if info_hash in blocked_hashes:
                return TorrentSelection(release=release, coverage=set())
            coverage = (
                torrent_coverage(metadata, release.title, chapters, series)
                if metadata
                else coverage_from_text(release.title, chapters)
            )
        except Exception:
            coverage = coverage_from_text(release.title, chapters)
        return TorrentSelection(release=release, coverage=coverage & wanted)

    inspected = await asyncio.gather(*(inspect(item) for item in candidates))
    useful = [selection for selection in inspected if selection.coverage]
    if not useful:
        return None
    return max(
        useful,
        key=lambda selection: (
            len(selection.coverage),
            sum(is_special_chapter(number) for number in selection.coverage),
            selection.release.seeders,
            -selection.release.size_bytes,
        ),
    )
