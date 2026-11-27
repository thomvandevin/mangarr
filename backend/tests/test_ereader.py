import io
import os
import zipfile

from PIL import Image

from mangarr.library.ereader import BookInfo, Contents, chapter_bundles, decode, mirror, plain_text, screen_pages


def _cbz(path, pages, comicinfo="<ComicInfo><Manga>YesAndRightToLeft</Manga></ComicInfo>"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ComicInfo.xml", comicinfo)
        for n, (size, colour) in enumerate(pages, start=1):
            data = io.BytesIO()
            Image.new("RGB", size, colour).save(data, "PNG")
            zf.writestr(f"{n:03d}.png", data.getvalue())


def test_spread_is_split_in_reading_order_and_strip_is_cut():
    spread = Image.new("RGB", (2000, 1400), "white")
    spread.paste("black", (1000, 0, 2000, 1400))
    right, left = screen_pages(spread, 1072, 1448, rtl=True)
    assert right.getpixel((10, 10)) == (0, 0, 0) and left.getpixel((10, 10)) == (255, 255, 255)
    assert screen_pages(spread, 1072, 1448, rtl=False)[0].getpixel((10, 10)) == (255, 255, 255)

    strip = screen_pages(Image.new("RGB", (720, 4000)), 1072, 1448, rtl=True)
    assert [p.size for p in strip] == [(720, 972)] * 4 + [(720, 112)]
    assert [p.size for p in screen_pages(Image.new("RGB", (2144, 2896)), 1072, 1448, True)] == [(1072, 1448)]


def test_mirror_follows_the_library(tmp_path):
    root, target = tmp_path / "manga", tmp_path / "kobo"
    one, two = root / "A" / "A - Ch. 0001.cbz", root / "B" / "B - Ch. 0001.cbz"
    _cbz(one, [((900, 1300), "red"), ((1800, 1300), "blue")])
    _cbz(two, [((900, 1300), "red")], comicinfo="<ComicInfo/>")
    folders = [(root, root / "A"), (root, root / "B")]
    mirror(folders, target, 1072, 1448, 75)
    with zipfile.ZipFile(target / "A" / "A - Ch. 0001.epub") as epub:
        names = epub.namelist()
        assert names[0] == "mimetype" and names.count("p0003.jpg") == 1 and "p0004.jpg" not in names
        opf = epub.read("content.opf").decode()
        assert 'page-progression-direction="rtl"' in opf and "<dc:title>A - Ch. 0001</dc:title>" in opf
    with zipfile.ZipFile(target / "B" / "B - Ch. 0001.epub") as epub:
        assert 'page-progression-direction="ltr"' in epub.read("content.opf").decode()

    made = (target / "A" / "A - Ch. 0001.epub").stat().st_mtime_ns
    _cbz(one, [((900, 1300), "green")])
    os.utime(one, ns=(made + 10**9, made + 10**9))
    two.unlink()
    mirror(folders, target, 1072, 1448, 75)
    assert not (target / "B").exists()
    with zipfile.ZipFile(target / "A" / "A - Ch. 0001.epub") as epub:
        assert "p0002.jpg" not in epub.namelist()
    _cbz(two, [((900, 1300), "red")])
    mirror(folders[:1], target, 1072, 1448, 75)
    assert not (target / "B").exists()

    for path in root.iterdir():
        path.rename(tmp_path / path.name)
    mirror(folders, target, 1072, 1448, 75)
    assert (target / "A" / "A - Ch. 0001.epub").exists()


def test_mirror_refuses_a_target_inside_the_library(tmp_path):
    root = tmp_path / "manga"
    _cbz(root / "A" / "A - Ch. 0001.cbz", [((900, 1300), "red")])
    kept = root / "A" / "mine.epub"
    kept.write_bytes(b"x")
    mirror([(root, root / "A")], root, 1072, 1448, 75)
    assert kept.exists() and not (root / "A" / "A - Ch. 0001.epub").exists()


def test_copies_carry_series_metadata_and_follow_its_changes(tmp_path):
    root, target = tmp_path / "manga", tmp_path / "kobo"
    folder = root / "A"
    _cbz(folder / "A - Vol. 03.cbz", [((900, 1300), "red")])
    _cbz(folder / "A - Ch. 0096.1.cbz", [((900, 1300), "red")])
    info = {folder: BookInfo("A & B", ("Kamome Shirahama",))}
    mirror([(root, folder)], target, 1072, 1448, 75, info)
    copy = target / "A" / "A - Vol. 03.epub"
    with zipfile.ZipFile(copy) as epub:
        opf = epub.read("content.opf").decode()
        page = epub.read("p0001.jpg")
    assert '<meta property="belongs-to-collection" id="series">A &amp; B</meta>' in opf
    assert 'property="group-position">3</meta>' in opf
    assert "<dc:creator>Kamome Shirahama</dc:creator>" in opf
    with zipfile.ZipFile(target / "A" / "A - Ch. 0096.1.epub") as epub:
        assert 'property="group-position">96.1</meta>' in epub.read("content.opf").decode()

    info[folder] = BookInfo("A & B", ("Kamome Shirahama",), plain_text("A witch<br><br>story &amp; more"))
    mirror([(root, folder)], target, 1072, 1448, 75, info)
    with zipfile.ZipFile(copy) as epub:
        assert epub.namelist()[0] == "mimetype" and epub.read("p0001.jpg") == page
        assert "<dc:description>A witch story &amp; more</dc:description>" in epub.read("content.opf").decode()


def test_covers_label_volumes_and_front_loose_chapters(tmp_path):
    root, target = tmp_path / "manga", tmp_path / "kobo"
    folder = root / "A"
    released, merged, loose = (folder / f"A - {n}.cbz" for n in ("Vol. 01", "Vol. 02", "Ch. 0011"))
    for src in (released, merged, loose):
        _cbz(src, [((900, 1300), "red"), ((900, 1300), "green")], comicinfo="<ComicInfo/>")
    art = tmp_path / "002.jpg"
    Image.new("RGB", (700, 1000), "blue").save(art, "JPEG")
    info = {folder: BookInfo("A", art=art)}
    contents = {merged: Contents(((6, "Six"), (7, "")), merged=True, art=art), loose: Contents(((11, "The End"),))}
    mirror([(root, folder)], target, 1072, 1448, 75, info, contents)

    def copy(name):
        with zipfile.ZipFile(target / "A" / f"A - {name}.epub") as epub:
            pages = sorted(n for n in epub.namelist() if n.endswith(".jpg"))
            first = Image.open(io.BytesIO(epub.read(pages[0]))).convert("RGB")
            return len(pages), first.getpixel((10, 10)), first.getpixel((10, 1440)), epub.read("content.opf").decode()

    count, top, bottom, _ = copy("Vol. 01")
    assert count == 2 and top[0] > 200 and sum(bottom) < 60  # its own cover, labelled
    count, top, bottom, opf = copy("Vol. 02")
    assert count == 3 and top[2] > 200 and sum(bottom) < 60
    assert "<dc:description>Chapters 6-7: 6. Six.</dc:description>" in opf
    count, top, bottom, opf = copy("Ch. 0011")
    assert count == 3 and top[2] > 200 and min(bottom) > 200
    assert "<dc:description>Chapter 11: The End.</dc:description>" in opf

    made = (target / "A" / "A - Vol. 02.epub").stat().st_mtime_ns
    mirror([(root, folder)], target, 1072, 1448, 75, info, contents)
    assert (target / "A" / "A - Vol. 02.epub").stat().st_mtime_ns == made
    os.utime(art, ns=(made + 10**9, made + 10**9))
    mirror([(root, folder)], target, 1072, 1448, 75, info, contents)
    assert (target / "A" / "A - Vol. 02.epub").stat().st_mtime_ns != made


def test_loose_chapters_are_bundled_once_their_range_is_out(tmp_path):
    root, target = tmp_path / "manga", tmp_path / "kobo"
    folder = root / "W"
    colours = {"0001": "red", "0002": "green", "0002.5": "blue", "0003": "red", "0004": "red", "0005": "red"}
    held = {}
    for label, colour in colours.items():
        _cbz(folder / f"W - Ch. {label}.cbz", [((700, 1280), colour)], comicinfo="<ComicInfo/>")
        held[folder / f"W - Ch. {label}.cbz"] = [(float(label), f"Part {label}")]

    def run():
        bundles = chapter_bundles(held, 2)
        for name, parts in bundles.items():
            held[name] = [chapter for part in parts for chapter in held[part]]
        contents = {path: Contents(tuple(chapters)) for path, chapters in held.items()}
        mirror([(root, folder)], target, 1072, 1448, 75, {folder: BookInfo("W")}, contents, bundles)
        return sorted(p.name for p in (target / "W").iterdir())

    assert run() == ["W - Ch. 0001-0002.5.epub", "W - Ch. 0003-0004.epub", "W - Ch. 0005.epub"]
    with zipfile.ZipFile(target / "W" / "W - Ch. 0001-0002.5.epub") as epub:
        pages = [Image.open(io.BytesIO(epub.read(f"p{n:04d}.jpg"))).convert("RGB").getpixel((10, 10)) for n in (2, 3, 4)]
        opf = epub.read("content.opf").decode()
        assert "p0005.jpg" not in epub.namelist()
    assert [max(range(3), key=pixel.__getitem__) for pixel in pages] == [0, 1, 2]  # red, green, blue
    assert 'property="group-position">1</meta>' in opf and "<dc:description>Chapters 1-2.5: 1. Part 0001" in opf

    _cbz(folder / "W - Ch. 0006.cbz", [((700, 1280), "red")], comicinfo="<ComicInfo/>")
    held[folder / "W - Ch. 0006.cbz"] = [(6.0, "")]
    assert run()[-1] == "W - Ch. 0005-0006.epub"


def test_truncated_page_is_still_decoded():
    data = io.BytesIO()
    Image.effect_noise((600, 900), 64).save(data, "JPEG")
    assert decode(data.getvalue()[:-200]).size == (600, 900)
