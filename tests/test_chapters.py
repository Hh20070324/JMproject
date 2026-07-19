from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    SearchMode,
    SearchRequest,
)
from jm_downloader.search import SearchResponseError, SearchService


class FakeChapterClient:
    def __init__(self, *, album=None, page=None):
        self.album = album
        self.page = page
        self.calls = []

    def get_album_detail(self, album_id):
        self.calls.append(("get_album_detail", album_id))
        return self.album

    def search_site(self, query, page):
        self.calls.append(("search_site", query, page))
        return self.page


def make_album(
    *,
    album_id="123",
    title=None,
    name="Album title",
    episode_list=(),
):
    return SimpleNamespace(
        album_id=album_id,
        title=title,
        name=name,
        authors=[],
        tags=[],
        episode_list=list(episode_list),
    )


class ChapterSnapshotTests(unittest.TestCase):
    def test_chapter_models_are_frozen_slotted_value_objects(self):
        chapter = ChapterSnapshot("301", 2, "Second chapter")
        catalog = ChapterCatalogSnapshot(
            "123",
            "Album title",
            (chapter,),
        )

        self.assertEqual(chapter.photo_id, "301")
        self.assertEqual(chapter.index, 2)
        self.assertEqual(chapter.title, "Second chapter")
        self.assertEqual(catalog.album_id, "123")
        self.assertEqual(catalog.title, "Album title")
        self.assertEqual(catalog.chapters, (chapter,))
        self.assertIsInstance(catalog.chapters, tuple)

        for value, attribute in (
            (chapter, "title"),
            (catalog, "chapters"),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, attribute, None)
                self.assertFalse(hasattr(value, "__dict__"))


class ChapterServiceTests(unittest.TestCase):
    def test_fetch_chapters_sorts_by_index_and_normalizes_values(self):
        episode_list = [
            ("303", "3", "  Finale  "),
            ("301", "1", " \t "),
            ("302", "2", "Middle\nchapter"),
        ]
        album = make_album(
            album_id="123",
            title="  Catalog\n title ",
            episode_list=episode_list,
        )
        client = FakeChapterClient(album=album)

        catalog = SearchService(
            client_factory=lambda: client
        ).fetch_chapters(" JM00123 ")

        self.assertEqual(client.calls, [("get_album_detail", "123")])
        self.assertEqual(catalog.album_id, "123")
        self.assertEqual(catalog.title, "Catalog title")
        self.assertEqual(
            catalog.chapters,
            (
                ChapterSnapshot("301", 1, "第 1 章"),
                ChapterSnapshot("302", 2, "Middle chapter"),
                ChapterSnapshot("303", 3, "Finale"),
            ),
        )
        self.assertEqual(episode_list[0][2], "  Finale  ")

    def test_fetch_chapters_rejects_nonpositive_or_duplicate_identity(self):
        invalid_episode_lists = (
            (("301", "0", "Zero"),),
            (("301", "1", "First"), ("302", "1", "Duplicate index")),
            (("301", "1", "First"), ("301", "2", "Duplicate id")),
        )

        for episode_list in invalid_episode_lists:
            with self.subTest(episode_list=episode_list):
                service = SearchService(
                    client_factory=lambda episode_list=episode_list: (
                        FakeChapterClient(
                            album=make_album(episode_list=episode_list)
                        )
                    )
                )
                with self.assertRaises(SearchResponseError):
                    service.fetch_chapters("123")

    def test_fetch_chapters_keeps_missing_album_title_as_none(self):
        album = make_album(
            title=" \n ",
            name="\t",
            episode_list=(("123", "1", ""),),
        )

        catalog = SearchService(
            client_factory=lambda: FakeChapterClient(album=album)
        ).fetch_chapters("123")

        self.assertIsNone(catalog.title)
        self.assertEqual(
            catalog.chapters,
            (ChapterSnapshot("123", 1, "第 1 章"),),
        )

    def test_exact_search_reuses_detail_for_its_chapter_catalog(self):
        album = make_album(
            title="Exact title",
            episode_list=(
                ("301", "1", "First"),
                ("302", "2", "Second"),
            ),
        )
        client = FakeChapterClient(album=album)

        result = SearchService(client_factory=lambda: client).search(
            SearchRequest(SearchMode.EXACT_ID, "123")
        )

        self.assertEqual(client.calls, [("get_album_detail", "123")])
        self.assertEqual(
            result.items[0].chapter_catalog,
            ChapterCatalogSnapshot(
                "123",
                "Exact title",
                (
                    ChapterSnapshot("301", 1, "First"),
                    ChapterSnapshot("302", 2, "Second"),
                ),
            ),
        )

    def test_regular_search_results_do_not_claim_to_have_a_catalog(self):
        page = SimpleNamespace(
            content=(("123", {"name": "Search title"}),),
            total=1,
            page_count=1,
            is_single_album=False,
        )
        client = FakeChapterClient(page=page)

        result = SearchService(client_factory=lambda: client).search(
            SearchRequest(SearchMode.GENERAL, "query")
        )

        self.assertEqual(client.calls, [("search_site", "query", 1)])
        self.assertIsNone(result.items[0].chapter_catalog)

    def test_regular_single_album_redirect_still_has_no_catalog(self):
        album = make_album(
            episode_list=(("301", "1", "First"),),
        )
        page = SimpleNamespace(
            content=(("123", {"name": "Search title"}),),
            total=1,
            page_count=1,
            is_single_album=True,
            single_album=album,
        )

        result = SearchService(
            client_factory=lambda: FakeChapterClient(page=page)
        ).search(SearchRequest(SearchMode.GENERAL, "query"))

        self.assertIsNone(result.items[0].chapter_catalog)


if __name__ == "__main__":
    unittest.main()
