import io
import os
import zipfile

from PIL import Image

from mangarr.library.ereader import decode, mirror, screen_pages


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


def test_truncated_page_is_still_decoded():
    data = io.BytesIO()
    Image.effect_noise((600, 900), 64).save(data, "JPEG")
    assert decode(data.getvalue()[:-200]).size == (600, 900)
