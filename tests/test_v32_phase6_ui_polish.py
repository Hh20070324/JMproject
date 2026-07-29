import os
from pathlib import Path
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from jm_downloader.models import LibraryLayout
from jm_downloader.qt.icons import svg_icon
from jm_downloader.qt.pages.library_page import LibraryPage
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.library_item_card import LibraryItemCard
from tests.test_v32_phase3_local_reader_ui import (
    FakeLibraryController,
    library_item,
)


ROOT = Path(__file__).resolve().parents[1]
ICON_ROOT = ROOT / "jm_downloader" / "qt" / "resources" / "icons"
PADDING_ICONS = (
    "bookmark",
    "arrow-down",
    "dark-mode",
    "light-mode",
    "pause",
    "play",
    "stop",
    "user-check",
    "user-delete",
    "book",
    "menu",
    "scan",
)


class IconPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmed_icons_use_shared_padding_viewbox_and_render(self):
        for name in PADDING_ICONS:
            source = (ICON_ROOT / f"{name}.svg").read_text(encoding="utf-8")
            self.assertRegex(source, r'viewBox="0 0 10 10"')
            self.assertIn("currentColor", source)
            icon = svg_icon(name, "#2e7d57")
            self.assertFalse(icon.isNull(), name)
            self.assertFalse(icon.pixmap(32, 32).isNull(), name)

    def test_library_card_updates_read_manage_and_scan_semantics(self):
        managed = library_item("123", LibraryLayout.MANAGED, images=True)
        legacy = library_item("123", LibraryLayout.LEGACY, images=True)
        card = LibraryItemCard(managed)
        managed_key = card.chapter_button.icon().cacheKey()

        self.assertEqual(card.read_button.toolTip(), "在应用内阅读本地完整章节")
        self.assertEqual(card.chapter_button.toolTip(), "管理章节")
        self.assertNotEqual(
            card.read_button.icon().cacheKey(),
            card.open_images_button.icon().cacheKey(),
        )
        card.update_item(legacy)

        self.assertFalse(card.read_button.isVisible())
        self.assertEqual(card.chapter_button.toolTip(), "识别章节（可能访问网络）")
        self.assertNotEqual(card.chapter_button.icon().cacheKey(), managed_key)
        self.assertNotEqual(
            card.chapter_button.icon().cacheKey(),
            card.open_images_button.icon().cacheKey(),
        )
        card.close()

    def test_existing_game_icon_license_notice_still_covers_resources(self):
        notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        license_file = ROOT / "LICENSES" / "Game-Icon-Pack-CC0-1.0.txt"

        self.assertIn("Game-Icon-Pack-CC0-1.0.txt", notice)
        self.assertTrue(license_file.is_file())
        self.assertIn("CC0", license_file.read_text(encoding="utf-8"))


class LibraryFilterSizingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        item = library_item("123", LibraryLayout.MANAGED, images=True)
        self.controller = FakeLibraryController((item,))
        self.page = LibraryPage(self.controller)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()

    @staticmethod
    def _global_rect(widget):
        rect = widget.rect()
        top_left = widget.mapToGlobal(QPoint(0, 0))
        rect.moveTopLeft(top_left)
        return rect

    def test_filter_buttons_use_text_width_instead_of_one_fixed_width(self):
        widths = {}
        for value, text in self.page.FILTERS:
            button = self.page.filter_button(value)
            required = button.fontMetrics().horizontalAdvance(text) + 28
            widths[value] = button.width()
            self.assertGreaterEqual(button.width(), max(52, required))
            self.assertEqual(button.text(), text)

        self.assertGreater(widths["pdf"], widths["all"])
        self.assertGreater(widths["pdf"], 68)

    def test_filter_buttons_remeasure_after_font_change(self):
        font = QFont(self.page.font())
        font.setPointSize(max(18, font.pointSize() + 6))
        self.page.setFont(font)
        self.app.processEvents()

        for value, text in self.page.FILTERS:
            button = self.page.filter_button(value)
            required = button.fontMetrics().horizontalAdvance(text) + 28
            self.assertGreaterEqual(button.width(), max(52, required))

    def test_filter_toolbar_does_not_overlap_at_two_layouts_and_four_sizes(self):
        for theme in ("light", "dark"):
            ThemeManager(theme).apply()
            for width in (760, 950, 1200, 1520):
                self.page.resize(width, 700)
                self.page._reflow_toolbar()
                self.app.processEvents()
                targets = (
                    self.page.search_input,
                    self.page.filter_segment,
                    self.page.sort_button,
                    self.page.select_button,
                    self.page.refresh_button,
                )
                rects = [self._global_rect(widget) for widget in targets]
                for index, first in enumerate(rects):
                    for second in rects[index + 1 :]:
                        self.assertFalse(first.intersects(second))


if __name__ == "__main__":
    unittest.main()
