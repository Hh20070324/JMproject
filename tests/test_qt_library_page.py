import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QEventLoop, QTimer, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import LibraryItem
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.pages import LibraryPage
from jm_downloader.qt.theme import ThemeManager


class FakeLibraryController(QObject):
    items_reset = Signal(object)
    loading_changed = Signal(bool)
    busy_albums_changed = Signal(object)
    active_albums_changed = Signal(object)
    operation_succeeded = Signal(str, str)
    command_failed = Signal(str, str, str)

    def __init__(self, items=None):
        super().__init__()
        self.items = list(items or [])
        self.active = frozenset()
        self.busy = frozenset()
        self.calls = []
        self.refresh_count = 0
        self.pending = False

    def list_items(self):
        return list(self.items)

    def active_album_ids(self):
        return self.active

    def busy_album_ids(self):
        return self.busy

    def has_pending_mutations(self):
        return self.pending

    def refresh(self):
        self.refresh_count += 1

    def open_item(self, album_id, kind):
        self.calls.append(("open", album_id, kind))

    def rebuild_pdf(self, album_id):
        self.calls.append(("rebuild", album_id))

    def delete_item(self, album_id, kind):
        self.calls.append(("delete", album_id, kind))


class LibraryPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["library-page-tests"])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.image_path = self.root / "Pictures" / "10" / "1" / "1.png"
        self.image_path.parent.mkdir(parents=True)
        image = QImage(80, 120, QImage.Format.Format_RGB32)
        image.fill(0xFF247A52)
        self.assertTrue(image.save(str(self.image_path)))
        self.items = [
            self._item("10", images=True, pdf=False),
            self._item("20", images=False, pdf=True),
            self._item("30", images=True, pdf=True),
        ]
        self.controller = FakeLibraryController(self.items)
        self.page = LibraryPage(self.controller)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.resize(1000, 700)
        self.page.show()
        self.app.processEvents()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_activate_search_filter_and_responsive_grid(self):
        self.page.activate()
        self.assertEqual(self.controller.refresh_count, 1)
        self.assertEqual(self.page.visible_album_ids, ("10", "20", "30"))
        self.assertEqual(self.page.column_count, 2)

        self.page.search_input.setText("JM 20")
        self.assertEqual(self.page.visible_album_ids, ("20",))
        self.page.search_input.clear()

        self.page.filter_button("images").click()
        self.assertEqual(self.page.visible_album_ids, ("10", "30"))
        self.page.filter_button("pdf").click()
        self.assertEqual(self.page.visible_album_ids, ("20", "30"))

        self.page.resize(600, 600)
        self.app.processEvents()
        self.app.processEvents()
        self.assertEqual(self.page.column_count, 1)

    def test_card_actions_and_activity_state(self):
        card = self.page.item_card("30")
        card.open_images_button.click()
        card.open_pdf_button.click()

        self.assertEqual(
            self.controller.calls,
            [("open", "30", "images"), ("open", "30", "pdf")],
        )
        self.controller.active = frozenset({"30"})
        self.controller.active_albums_changed.emit(self.controller.active)
        self.assertFalse(card.rebuild_button.isEnabled())
        self.assertFalse(card.delete_button.isEnabled())
        self.assertTrue(card.open_pdf_button.isEnabled())
        self.assertEqual(card.state_label.text(), "下载中")

        self.controller.active = frozenset()
        self.controller.active_albums_changed.emit(self.controller.active)
        self.controller.busy = frozenset({"30"})
        self.controller.busy_albums_changed.emit(self.controller.busy)
        self.assertEqual(card.state_label.text(), "处理中")

    def test_delete_and_rebuild_require_native_confirmation(self):
        card = self.page.item_card("30")
        with patch(
            "jm_downloader.qt.pages.library_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            card.delete_images_action.trigger()
            card.rebuild_button.click()
        self.assertEqual(self.controller.calls, [])

        with patch(
            "jm_downloader.qt.pages.library_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            card.delete_all_action.trigger()
            card.rebuild_button.click()

        self.assertEqual(
            self.controller.calls,
            [("delete", "30", "all"), ("rebuild", "30")],
        )

    def test_loading_empty_error_and_thumbnail_states(self):
        self.assertTrue(
            self._wait_until(
                lambda: not self.page.item_card("10").preview.pixmap().isNull()
            )
        )

        self.controller.items_reset.emit([])
        self.assertEqual(self.page.state_label.text(), "本地漫画库为空")
        self.controller.loading_changed.emit(True)
        self.assertTrue(self.page.loading_bar.isVisible())
        self.controller.loading_changed.emit(False)

        self.controller.command_failed.emit("refresh", "", "目录无法读取")
        self.assertEqual(self.page.state_label.text(), "本地漫画库读取失败")
        self.assertTrue(self.page.retry_button.isVisible())

    def test_main_window_blocks_close_during_library_mutation(self):
        self.controller.pending = True
        window = MainWindow(
            ThemeManager(),
            library_controller=self.controller,
        )
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        self.app.processEvents()

        with patch(
            "jm_downloader.qt.main_window.QMessageBox.information"
        ) as information:
            window.close()
            self.app.processEvents()
        self.assertTrue(window.isVisible())
        information.assert_called_once()

        self.controller.pending = False
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def _item(self, album_id: str, images: bool, pdf: bool) -> LibraryItem:
        preview = self.image_path if images else None
        pdf_path = self.root / "PDFs" / f"{album_id}.pdf" if pdf else None
        return LibraryItem(
            album_id=album_id,
            chapter_count=1 if images else 0,
            image_count=2 if images else 0,
            image_size=2048 if images else 0,
            preview_path=preview,
            pdf_path=pdf_path,
            pdf_size=4096 if pdf else 0,
        )

    def _wait_until(self, predicate, timeout_ms: int = 3000) -> bool:
        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        return predicate()


if __name__ == "__main__":
    unittest.main()
