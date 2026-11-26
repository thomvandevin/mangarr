"""E-reader copies: every CBZ in the library as a fixed-layout EPUB with
pages sized for the reader's screen, in a folder that mirrors the library.
Komga's Kobo sync only delivers EPUB."""

import asyncio
import hashlib
import io
import json
import logging
import re
import time
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from html import escape, unescape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFile, ImageFont, ImageOps
from sqlalchemy import select

from .. import settings_service
from ..automation import MERGED_FILE_SOURCE
from ..config import config
from ..db import session_scope
from ..komga import Komga, from_settings as komga_from_settings
from ..models import Chapter, RootFolder, Series, SeriesSourceLink
from ..sources.mangadex import source as mangadex
from .archive_members import archive_files, archive_pages
from .scanner import root_unavailable, series_dir

log = logging.getLogger(__name__)

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
    "</container>"
)
NAV = (
    '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{title}</title></head><body>'
    '<nav epub:type="toc"><ol><li><a href="p0001.xhtml">{title}</a></li></ol></nav></body></html>'
)
COVER = ' properties="cover-image"'
PAGE = (
    '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
    '<meta name="viewport" content="width={w}, height={h}"/><title>{n}</title>'
    "<style>body{{margin:0}}img{{display:block}}</style></head>"
    '<body><img src="p{n:04d}.jpg" width="{w}" height="{h}" alt=""/></body></html>'
)


@dataclass(frozen=True)
class BookInfo:
    """Series metadata written into every copy, so Komga (and the Kobo
    through it) shows the series, its order, the authors and a summary."""

    series: str = ""
    authors: tuple[str, ...] = ()
    description: str = ""
    art: Path | None = None  # shown on the cover of copies without art of their own


@dataclass(frozen=True)
class Contents:
    """What one archive holds: its chapters as (number, title) in order and,
    for a volume Mangarr merged, the cover art found for that volume."""

    chapters: tuple[tuple[float, str], ...] = ()
    merged: bool = False
    art: Path | None = None


# bump to rebuild every copy's cover
COVER_STYLE = 1


@dataclass(frozen=True)
class Cover:
    """The generated first page of a copy. `band` puts a label naming the
    series and volume under the art, which without `art` is the archive's own
    first page (a downloaded volume opens with its cover). Otherwise a card
    shows the art above the heading, number and title."""

    series: str
    heading: str
    number: str
    title: str = ""
    art: Path | None = None
    band: bool = False

    @property
    def own_page(self) -> bool:
        return self.band and self.art is None


def cover_for(src: Path, book: BookInfo, contents: Contents) -> Cover | None:
    number = series_position(src)
    if not book.series or not number:
        return None
    if re.search(r" - Vol\. ", src.stem):
        if contents.art or not contents.merged:
            return Cover(book.series, "Volume", number, art=contents.art, band=True)
        return Cover(book.series, "Volume", number, art=book.art)
    title = contents.chapters[0][1] if len(contents.chapters) == 1 else ""
    return Cover(book.series, "Chapter", number, title, book.art)


def cover_signature(cover: Cover | None) -> str:
    return '<meta name="mangarr:cover" content="%s"/>' % hashlib.sha1(
        f"{COVER_STYLE}{cover!r}".encode()
    ).hexdigest()[:16]


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _fitted(draw: ImageDraw.ImageDraw, text: str, size: int, width: float) -> ImageFont.FreeTypeFont:
    while size > 12 and draw.textlength(text, font=_font(size)) > width:
        size -= 2
    return _font(size)


def _wrapped(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: float, most: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= width:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    if len(lines) > most:
        lines = lines[:most]
        lines[-1] += " ..."
    return lines


def cover_page(cover: Cover, art: bytes | None, width: int, height: int) -> bytes:
    """The cover rendered as a screen-sized PNG."""
    page = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(page)
    unit, margin = width / 1072, width // 16
    bold = max(1, round(2 * unit))
    if cover.band:
        with decode(art) as image:
            page.paste(ImageOps.fit(image.convert("RGB"), (width, height)))
        band = height // 9
        middle = height - band // 2
        draw.rectangle((0, height - band, width, height), fill="black")
        label, font = f"Vol. {cover.number}", _font(band * 3 // 5)
        draw.text((width - margin, middle), label, font=font, fill="white", anchor="rm",
                  stroke_width=bold, stroke_fill="white")
        room = width - 3 * margin - draw.textlength(label, font=font)
        draw.text((margin, middle), cover.series, font=_fitted(draw, cover.series, band * 2 // 5, room),
                  fill="white", anchor="lm")
    else:
        art_height = height * 9 // 20
        if art:
            with decode(art) as image:
                page.paste(ImageOps.fit(image.convert("RGB"), (width, art_height), centering=(0.5, 0.3)))
        else:
            draw.rectangle((0, 0, width, art_height), fill="black")
        draw.rectangle((0, art_height, width, art_height + round(12 * unit)), fill="black")
        room = width - 2 * margin
        y = art_height + round(60 * unit)
        draw.text((margin, y), cover.series.upper(), font=_fitted(draw, cover.series.upper(), round(52 * unit), room),
                  fill="black", stroke_width=1, stroke_fill="black")
        y += round(72 * unit)
        draw.text((margin, y), cover.heading, font=_font(round(64 * unit)), fill="black")
        y += round(64 * unit)
        draw.text((margin, y), cover.number, font=_fitted(draw, cover.number, round(260 * unit), room),
                  fill="black", stroke_width=2 * bold, stroke_fill="black")
        y += round(290 * unit)
        font = _font(round(52 * unit))
        for line in _wrapped(draw, cover.title, font, room, 3):
            draw.text((margin, y), line, font=font, fill="black")
            y += round(68 * unit)
    out = io.BytesIO()
    page.save(out, "PNG")
    return out.getvalue()


def contents_text(chapters: tuple[tuple[float, str], ...]) -> str:
    if len(chapters) == 1:
        number, title = chapters[0]
        return f"Chapter {number:g}" + (f": {title}" if title else "")
    if not chapters:
        return ""
    lines = [f"Chapters {chapters[0][0]:g}-{chapters[-1][0]:g}"]
    lines += [f"{number:g}. {title}" for number, title in chapters if title]
    return "\n".join(lines)


def plain_text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def series_position(src: Path) -> str:
    """A volume sorts by its volume number and a loose chapter by its
    chapter number, which comes after the volumes holding earlier chapters."""
    match = re.search(r" - Vol\. (\d+(?:\.\d+)?)$", src.stem) or re.search(r" - Ch\. (\d+(?:\.\d+)?)", src.stem)
    return f"{float(match.group(1)):g}" if match else ""


def metadata_xml(src: Path, info: BookInfo, cover: Cover | None = None) -> str:
    modified = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc)
    position = series_position(src)
    series = (
        f'<meta property="belongs-to-collection" id="series">{escape(info.series)}</meta>'
        '<meta refines="#series" property="collection-type">series</meta>'
        f'<meta refines="#series" property="group-position">{position}</meta>'
    ) if info.series and position else ""
    return (
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="id">urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, src.as_posix())}</dc:identifier>'
        f"<dc:title>{escape(src.stem)}</dc:title><dc:language>en</dc:language>"
        + "".join(f"<dc:creator>{escape(name)}</dc:creator>" for name in info.authors)
        + (f"<dc:description>{escape(info.description)}</dc:description>" if info.description else "")
        + series
        + f'<meta property="dcterms:modified">{modified:%Y-%m-%dT%H:%M:%SZ}</meta>'
        '<meta property="rendition:layout">pre-paginated</meta>'
        '<meta property="rendition:spread">none</meta><meta name="cover" content="i0001"/>'
        + cover_signature(cover)
        + "</metadata>"
    )


def refresh_metadata(src: Path, dest: Path, info: BookInfo, cover: Cover | None = None) -> bool:
    """Swap the metadata of an existing copy without re-encoding its pages.
    Returns whether the copy changed."""
    with zipfile.ZipFile(dest) as epub:
        opf = epub.read("content.opf").decode()
        updated = re.sub(
            r"<metadata .*?</metadata>", lambda _: metadata_xml(src, info, cover), opf, count=1, flags=re.S
        )
        if updated == opf:
            return False
        tmp = dest.with_suffix(".epub.partial")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as out:
                for entry in epub.infolist():
                    out.writestr(entry, updated if entry.filename == "content.opf" else epub.read(entry))
            tmp.rename(dest)
        finally:
            tmp.unlink(missing_ok=True)
    return True


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    try:
        image.load()
    except OSError:
        # a page cut off a few bytes early still shows everything above the cut
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
    return image


def screen_pages(image: Image.Image, width: int, height: int, rtl: bool) -> list[Image.Image]:
    """One source page as the pages shown on a width x height screen: a
    spread becomes its two halves in reading order and a webtoon strip is
    cut into screen-high pieces."""
    w, h = image.size
    if w > h:
        halves = [image.crop((0, 0, w // 2, h)), image.crop((w // 2, 0, w, h))]
        parts = halves[::-1] if rtl else halves
    elif h * width > 1.5 * w * height:
        # plain cuts: a panel or speech bubble can end up split over two pages
        step = w * height // width
        parts = [image.crop((0, top, w, min(top + step, h))) for top in range(0, h, step)]
    else:
        parts = [image]
    pages = []
    for part in parts:
        if part.mode not in ("L", "RGB"):
            part = part.convert("RGB")
        part.thumbnail((width, height), Image.Resampling.LANCZOS)
        pages.append(part)
    return pages


def write_epub(
    src: Path, dest: Path, width: int, height: int, quality: int, info: BookInfo = BookInfo(),
    cover: Cover | None = None,
) -> None:
    title = escape(src.stem)
    tmp = dest.with_suffix(".epub.partial")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as cbz, zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as epub:
            files = list(archive_files(cbz.infolist()))
            rtl = any(
                path.name.casefold() == "comicinfo.xml" and b"RightToLeft" in cbz.read(entry)
                for entry, path in files
            )
            epub.writestr("mimetype", "application/epub+zip")
            epub.writestr("META-INF/container.xml", CONTAINER)
            epub.writestr("nav.xhtml", NAV.format(title=title))
            count = 0
            reads = [partial(cbz.read, entry) for entry, _ in sorted(archive_pages(files), key=lambda f: f[1].as_posix())]
            if cover:
                art = reads.pop(0) if cover.own_page else cover.art.read_bytes if cover.art else None
                reads.insert(0, lambda: cover_page(cover, art() if art else None, width, height))
            for read in reads:
                with decode(read()) as image:
                    for page in screen_pages(image, width, height, rtl):
                        count += 1
                        data = io.BytesIO()
                        page.save(data, "JPEG", quality=quality, optimize=True)
                        epub.writestr(f"p{count:04d}.jpg", data.getvalue())
                        epub.writestr(f"p{count:04d}.xhtml", PAGE.format(n=count, w=page.width, h=page.height))
            if not count:
                raise ValueError("no pages")
            numbers = range(1, count + 1)
            epub.writestr("content.opf", (
                '<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" '
                'version="3.0" unique-identifier="id" '
                'prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
                + metadata_xml(src, info, cover)
                + '<manifest><item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
                + "".join(
                    f'<item id="i{n:04d}" href="p{n:04d}.jpg" media-type="image/jpeg"'
                    f'{COVER if n == 1 else ""}/>'
                    f'<item id="p{n:04d}" href="p{n:04d}.xhtml" media-type="application/xhtml+xml"/>'
                    for n in numbers
                )
                + f'</manifest><spine page-progression-direction="{"rtl" if rtl else "ltr"}">'
                + "".join(f'<itemref idref="p{n:04d}"/>' for n in numbers)
                + "</spine></package>"
            ))
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)


def mirror(
    folders: list[tuple[Path, Path]], target: Path, width: int, height: int, quality: int,
    info: dict[Path, BookInfo] | None = None, contents: dict[Path, Contents] | None = None,
) -> bool:
    """Make `target` hold exactly one EPUB per CBZ in the (root, series
    folder) pairs, laid out as under the root, carrying the metadata `info`
    holds for each series folder and what `contents` knows of each archive.
    Returns whether any copy changed."""
    info, contents = info or {}, contents or {}
    wanted: dict[Path, tuple[Path, BookInfo]] = {}
    changed = False
    # copies inside the library would be picked up as library files, and
    # the cleanup below would delete EPUBs the user keeps there
    if any(target == root or root in target.parents or target in root.parents for root, _ in folders):
        log.warning("e-reader copies skipped: %s overlaps a library root folder", target)
        return changed
    for root, folder in folders:
        # an unmounted library must not empty the reader
        if problem := root_unavailable(root):
            log.warning("e-reader copies skipped: %s", problem)
            return changed
        for src in folder.rglob("*"):
            if src.suffix.lower() in {".cbz", ".zip"}:
                wanted[target / src.relative_to(root).with_suffix(".epub")] = (src, info.get(folder, BookInfo()))
    if target.is_dir():
        for stale in [p for p in target.rglob("*.epub") if p not in wanted]:
            stale.unlink()
            changed = True
        for folder in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    for dest, (src, book) in sorted(wanted.items()):
        held = contents.get(src, Contents())
        book = replace(book, description="\n\n".join(filter(None, (contents_text(held.chapters), book.description))))
        cover = cover_for(src, book, held)
        art = cover.art if cover else None
        try:
            if (
                not dest.exists()
                or dest.stat().st_mtime < max(src.stat().st_mtime, art.stat().st_mtime if art else 0)
                or not _has_cover(dest, cover)
            ):
                write_epub(src, dest, width, height, quality, book, cover)
                changed = True
            else:
                changed = refresh_metadata(src, dest, book, cover) or changed
        except Exception as exc:
            log.warning("e-reader copy of %s failed: %s", src, exc)
    return changed


def _has_cover(dest: Path, cover: Cover | None) -> bool:
    with zipfile.ZipFile(dest) as epub:
        return cover_signature(cover) in epub.read("content.opf").decode()


COVER_CHECK_SECONDS = 24 * 3600


async def volume_covers(series_id: int, mangadex_id: str, volumes: set[int]) -> dict[int, Path]:
    """Cached MangaDex cover per volume. Missing covers are looked up at
    most once a day per series, so a volume without art costs no requests."""
    folder = config.data_dir / "covers" / str(series_id)
    paths = {volume: folder / f"{volume:03d}.jpg" for volume in volumes}
    checked = folder / ".checked"
    missing = {volume for volume, path in paths.items() if not path.exists()}
    if missing and (not checked.exists() or time.time() - checked.stat().st_mtime > COVER_CHECK_SECONDS):
        folder.mkdir(parents=True, exist_ok=True)
        checked.touch()
        try:
            urls = await mangadex.volume_covers(mangadex_id)
            for volume in sorted(missing & urls.keys()):
                tmp = paths[volume].with_suffix(".partial")
                tmp.write_bytes(await mangadex.fetch_image(urls[volume]))
                tmp.rename(paths[volume])
        except Exception as exc:
            log.warning("volume covers for series %d failed: %s", series_id, exc)
    return {volume: path for volume, path in paths.items() if path.exists()}


async def sync_ereader_copies() -> None:
    async with session_scope() as session:
        values = await settings_service.get_all(session)
        folders, info, found, pending = [], {}, [], []
        for series, root in await session.execute(
            select(Series, RootFolder.path)
            .join(RootFolder, RootFolder.id == Series.root_folder_id)
            .where(Series.ereader_copy)
        ):
            folder = series_dir(Path(root), series)
            folders.append((Path(root), folder))
            info[folder] = BookInfo(
                series=series.title,
                authors=tuple(name for name in series.authors.splitlines() if name.strip()),
                description=plain_text(series.description),
            )
            held: dict[Path, list[tuple[float, str]]] = {}
            merged, highest = set(), 0
            for path, number, title, source, volume in await session.execute(
                select(Chapter.file_path, Chapter.number, Chapter.title, Chapter.file_source, Chapter.volume)
                .where(
                    Chapter.series_id == series.id, Chapter.downloaded.is_(True),
                    Chapter.excluded.is_(False), Chapter.file_path != "",
                )
                .order_by(Chapter.number)
            ):
                held.setdefault(Path(path), []).append((number, title))
                if source == MERGED_FILE_SOURCE:
                    merged.add(Path(path))
                highest = max(highest, volume or 0)
            link = (await session.execute(
                select(SeriesSourceLink.external_id)
                .where(SeriesSourceLink.series_id == series.id, SeriesSourceLink.source_name == mangadex.name)
            )).scalar_one_or_none()
            # downloaded volume releases bring their own cover; merged ones get MangaDex art
            by_volume = {int(m.group(1)): f for f in merged if (m := re.search(r" - Vol\. (\d+)$", f.stem))}
            found.append((series.id, folder, link, by_volume, highest, held, merged))
            if series.pending_progress:
                pending.append((series.id, folder.name, json.loads(series.pending_progress), by_volume))
    if not values["ereader_dir"]:
        return
    contents = {}
    for series_id, folder, link, by_volume, highest, held, merged in found:
        # the newest volume's art, released or not, fronts the loose chapters
        art = await volume_covers(series_id, link, {*by_volume, highest} - {0}) if link else {}
        if art:
            info[folder] = replace(info[folder], art=art[max(art)])
        covers = {by_volume[volume]: path for volume, path in art.items() if volume in by_volume}
        for path, chapters in held.items():
            contents[path] = Contents(tuple(chapters), path in merged, covers.get(path))
    changed = await asyncio.to_thread(
        mirror, folders, Path(values["ereader_dir"]),
        int(values["ereader_width"]), int(values["ereader_height"]), int(values["ereader_quality"]),
        info, contents,
    )
    if komga := komga_from_settings(values):
        await update_komga(*komga, changed, pending)


async def update_komga(
    komga: Komga, library_id: str, changed: bool,
    pending: list[tuple[int, str, dict[str, int | str], dict[int, Path]]],
) -> None:
    """Scan Komga's library when copies changed, then give each merged volume
    Komga now lists the progress read from its chapters."""
    if changed:
        try:
            await komga.scan(library_id)
        except Exception as exc:
            log.warning("Komga scan failed: %s", exc)
    for series_id, name, waiting, by_volume in pending:
        done = []
        try:
            books = await komga.series_books(library_id, name)
            for volume, read in waiting.items():
                src = by_volume.get(int(volume))
                book = books.get(src.stem) if src else None
                if not book or not (book.get("media") or {}).get("pagesCount"):
                    continue  # not scanned yet
                # the copy opens with a generated cover, ahead of the chapters
                page = None if read == "completed" else read + 1
                await komga.set_progress(book["id"], page)
                done.append(volume)
        except Exception as exc:
            log.warning("Komga progress for series %d failed: %s", series_id, exc)
        if done:
            async with session_scope() as session:
                series = await session.get(Series, series_id)
                left = {k: v for k, v in json.loads(series.pending_progress or "{}").items() if k not in done}
                series.pending_progress = json.dumps(left) if left else ""
                await session.commit()
