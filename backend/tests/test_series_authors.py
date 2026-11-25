from mangarr.metadata.anilist import AniListProvider
from mangarr.metadata.mangaupdates import MangaUpdatesProvider


def test_authors_are_the_story_and_art_credits():
    staff = {"edges": [
        {"role": "Story & Art", "node": {"name": {"full": "Kentarou Miura"}}},
        {"role": "Translator (English)", "node": {"name": {"full": "Duane Johnson"}}},
        {"role": "Original Story", "node": {"name": {"full": "Kouji Mori"}}},
        {"role": "Art", "node": {"name": {"full": "Kentarou Miura"}}},
    ]}
    anilist = AniListProvider.__new__(AniListProvider)._to_metadata({"id": 1, "title": {}, "staff": staff})
    assert anilist.authors == ["Kentarou Miura", "Kouji Mori"]

    record = {"series_id": 2, "authors": [{"name": "MIURA Kentarou", "type": "Author"},
                                          {"name": "MIURA Kentarou", "type": "Artist"}]}
    assert MangaUpdatesProvider.__new__(MangaUpdatesProvider)._to_metadata(record).authors == ["MIURA Kentarou"]
