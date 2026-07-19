import os
from types import SimpleNamespace
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    FavoriteItemSnapshot,
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from jm_downloader.qt.pages.download_page import DownloadPage
from jm_downloader.qt.pages.favorites_page import FavoritesPage


def catalog(album_id: str, count: int) -> ChapterCatalogSnapshot:
    return ChapterCatalogSnapshot(
        album_id,
        f"Title {album_id}",
        tuple(
            ChapterSnapshot(str(int(album_id) * 10 + index), index, f"Chapter {index}")
            for index in range(1, count + 1)
        ),
    )


class FakeChapterController(QObject):
    catalog_ready = Signal(int, object)
    catalog_failed = Signal(int, str, str)
    busy_changed = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.primed = []
        self._next_request_id = 0

    def request(self, album_id):
        self._next_request_id += 1
        self.requests.append((self._next_request_id, album_id))
        return self._next_request_id

    def prime(self, value):
        self.primed.append(value)

    def resolve(self, request_id, value):
        self.catalog_ready.emit(request_id, value)


class FakeDownloadController(QObject):
    tasks_reset = Signal(object)
    command_failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.added = []

    def list_tasks(self):
        return []

    def add_task(self, album_id, selected_chapter_ids=None):
        selected = (
            None
            if selected_chapter_ids is None
            else tuple(selected_chapter_ids)
        )
        self.added.append((album_id, selected))
        return SimpleNamespace(album_id=album_id)


class AcceptedSelectionDialog:
    selected_ids = ()

    def __init__(self, value, _parent=None):
        self.catalog = value

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected_chapter_ids(self):
        return tuple(self.selected_ids)

    def deleteLater(self):
        pass


class RejectedSelectionDialog(AcceptedSelectionDialog):
    def exec(self):
        return QDialog.DialogCode.Rejected


class ChapterDownloadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["chapter-download-integration-tests"]
        )

    def tearDown(self):
        for page in getattr(self, "pages", ()):
            page.dispose()
            page.close()
            page.deleteLater()
        self.app.processEvents()

    def setUp(self):
        self.pages = []
        self.download_controller = FakeDownloadController()
        self.chapter_controller = FakeChapterController()

    def make_download_page(self):
        page = DownloadPage(
            self.download_controller,
            chapter_catalog_controller=self.chapter_controller,
        )
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        self.pages.append(page)
        self.app.processEvents()
        return page

    def test_search_card_loads_catalog_and_single_chapter_queues_directly(self):
        page = self.make_download_page()
        request = SearchRequest(SearchMode.GENERAL, "title")
        page._search_generation = 1
        page._on_search_results(
            1,
            SearchPageSnapshot(
                request,
                1,
                1,
                (SearchResultSnapshot("123", "Title"),),
            ),
            False,
        )
        card = page.comic_cards[0]

        card.action_button.click()
        self.assertEqual(self.chapter_controller.requests, [(1, "123")])
        self.assertEqual(card.action_button.text(), "读取章节…")
        self.assertFalse(card.action_button.isEnabled())

        value = catalog("123", 1)
        self.chapter_controller.resolve(1, value)
        self.app.processEvents()

        self.assertEqual(
            self.download_controller.added,
            [("123", (value.chapters[0].photo_id,))],
        )
        self.assertTrue(card.task_present)

    def test_exact_result_reuses_catalog_and_multi_chapter_uses_selection(self):
        page = self.make_download_page()
        value = catalog("350234", 3)
        AcceptedSelectionDialog.selected_ids = (
            value.chapters[0].photo_id,
            value.chapters[2].photo_id,
        )
        page._chapter_flow.dialog_factory = AcceptedSelectionDialog
        request = SearchRequest(SearchMode.EXACT_ID, "350234")
        page._search_generation = 1
        page._on_search_results(
            1,
            SearchPageSnapshot(
                request,
                1,
                1,
                (
                    SearchResultSnapshot(
                        "350234",
                        "Title",
                        chapter_catalog=value,
                    ),
                ),
            ),
            False,
        )
        card = page.comic_cards[0]
        self.assertEqual(card.action_button.text(), "章节选择")

        card.action_button.click()

        self.assertEqual(self.chapter_controller.requests, [])
        self.assertEqual(self.chapter_controller.primed, [value])
        self.assertEqual(
            self.download_controller.added,
            [("350234", AcceptedSelectionDialog.selected_ids)],
        )

    def test_direct_input_and_favorite_use_the_same_catalog_flow(self):
        page = self.make_download_page()
        value = catalog("1449491", 2)
        AcceptedSelectionDialog.selected_ids = (value.chapters[1].photo_id,)
        page._chapter_flow.dialog_factory = AcceptedSelectionDialog
        page.download_input.setText("JM1449491")

        page.download_button.click()
        self.assertEqual(page.download_button.text(), "读取章节…")
        self.chapter_controller.resolve(1, value)
        self.app.processEvents()

        self.assertEqual(
            self.download_controller.added,
            [("1449491", AcceptedSelectionDialog.selected_ids)],
        )
        self.assertEqual(page.download_input.text(), "")
        self.assertEqual(page.view_tabs.currentIndex(), 1)

        favorite_downloads = FakeDownloadController()
        favorite_chapters = FakeChapterController()
        favorites_page = FavoritesPage(
            download_controller=favorite_downloads,
            chapter_catalog_controller=favorite_chapters,
        )
        favorites_page.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        favorites_page.show()
        self.pages.append(favorites_page)
        favorites_page._set_cards((FavoriteItemSnapshot("456", "Title"),))
        favorite_card = favorites_page.favorite_cards[0]

        favorite_card.action_button.click()
        self.assertEqual(favorite_chapters.requests, [(1, "456")])
        favorite_value = catalog("456", 1)
        favorite_chapters.resolve(1, favorite_value)
        self.app.processEvents()

        self.assertEqual(
            favorite_downloads.added,
            [("456", (favorite_value.chapters[0].photo_id,))],
        )
        self.assertTrue(favorite_card.task_present)

    def test_cancelled_direct_selection_keeps_input_ready_for_retry(self):
        page = self.make_download_page()
        value = catalog("789", 2)
        page._chapter_catalogs["789"] = value
        page._chapter_flow.dialog_factory = RejectedSelectionDialog
        page.download_input.setText("JM789")

        page.download_button.click()
        self.app.processEvents()

        self.assertEqual(self.download_controller.added, [])
        self.assertEqual(page.download_input.text(), "JM789")
        self.assertIsNone(page._direct_chapter_album_id)
        self.assertTrue(page.download_input.isEnabled())
        self.assertTrue(page.download_button.isEnabled())
        self.assertEqual(page.download_button.text(), "开始下载")


if __name__ == "__main__":
    unittest.main()
