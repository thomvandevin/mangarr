"""E-reader copies: every CBZ in the library as a fixed-layout EPUB with
pages sized for the reader's screen, in a folder that mirrors the library.
Komga's Kobo sync only delivers EPUB."""

import asyncio
import io
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path

from PIL import Image, ImageFile
from sqlalchemy import select

from .. import settings_service
from ..db import session_scope
from ..models import RootFolder, Series
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


def plain_text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def series_position(src: Path) -> str:
    """A volume sorts by its volume number and a loose chapter by its
    chapter number, which comes after the volumes holding earlier chapters."""
    match = re.search(r" - Vol\. (\d+(?:\.\d+)?)$", src.stem) or re.search(r" - Ch\. (\d+(?:\.\d+)?)", src.stem)
    return f"{float(match.group(1)):g}" if match else ""


def metadata_xml(src: Path, info: BookInfo) -> str:
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
        '<meta property="rendition:spread">none</meta><meta name="cover" content="i0001"/></metadata>'
    )


def refresh_metadata(src: Path, dest: Path, info: BookInfo) -> None:
    """Swap the metadata of an existing copy without re-encoding its pages."""
    with zipfile.ZipFile(dest) as epub:
        opf = epub.read("content.opf").decode()
        updated = re.sub(r"<metadata .*?</metadata>", lambda _: metadata_xml(src, info), opf, count=1, flags=re.S)
        if updated == opf:
            return
        tmp = dest.with_suffix(".epub.partial")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as out:
                for entry in epub.infolist():
                    out.writestr(entry, updated if entry.filename == "content.opf" else epub.read(entry))
            tmp.rename(dest)
        finally:
            tmp.unlink(missing_ok=True)


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
    src: Path, dest: Path, width: int, height: int, quality: int, info: BookInfo = BookInfo()
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
            for entry, _ in sorted(archive_pages(files), key=lambda f: f[1].as_posix()):
                with decode(cbz.read(entry)) as image:
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
                + metadata_xml(src, info)
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
    info: dict[Path, BookInfo] | None = None,
) -> None:
    """Make `target` hold exactly one EPUB per CBZ in the (root, series
    folder) pairs, laid out as under the root, carrying the metadata `info`
    holds for each series folder."""
    info = info or {}
    wanted: dict[Path, tuple[Path, BookInfo]] = {}
    # copies inside the library would be picked up as library files, and
    # the cleanup below would delete EPUBs the user keeps there
    if any(target == root or root in target.parents or target in root.parents for root, _ in folders):
        log.warning("e-reader copies skipped: %s overlaps a library root folder", target)
        return
    for root, folder in folders:
        # an unmounted library must not empty the reader
        if problem := root_unavailable(root):
            log.warning("e-reader copies skipped: %s", problem)
            return
        for src in folder.rglob("*"):
            if src.suffix.lower() in {".cbz", ".zip"}:
                wanted[target / src.relative_to(root).with_suffix(".epub")] = (src, info.get(folder, BookInfo()))
    if target.is_dir():
        for stale in [p for p in target.rglob("*.epub") if p not in wanted]:
            stale.unlink()
        for folder in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    for dest, (src, book) in sorted(wanted.items()):
        try:
            if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
                write_epub(src, dest, width, height, quality, book)
            else:
                refresh_metadata(src, dest, book)
        except Exception as exc:
            log.warning("e-reader copy of %s failed: %s", src, exc)


async def sync_ereader_copies() -> None:
    async with session_scope() as session:
        values = await settings_service.get_all(session)
        folders, info = [], {}
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
    if values["ereader_dir"]:
        await asyncio.to_thread(
            mirror, folders, Path(values["ereader_dir"]),
            int(values["ereader_width"]), int(values["ereader_height"]), int(values["ereader_quality"]),
            info,
        )
