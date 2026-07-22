from html import escape
import os
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from jm_downloader.models import SearchResultSnapshot
from jm_downloader.qt.theme import Theme, load_stylesheet
from jm_downloader.qt.widgets.search_result_card import SearchResultCard


def make_snapshot(**changes) -> SearchResultSnapshot:
    values = {
        "album_id": "1449491",
        "title": "测试漫画",
        "authors": ("测试作者",),
        "tags": ("全彩", "短篇"),
    }
    values.update(changes)
    return SearchResultSnapshot(**values)


def safe_tooltip(text: str) -> str:
    return f"<qt>{escape(text, quote=True)}</qt>"


class SearchResultCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["search-result-card-tests"]
        )

    def setUp(self):
        self.card = SearchResultCard(make_snapshot())
        self.card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.card.show()
        self.app.processEvents()

    def tearDown(self):
        self.card.close()
        self.card.deleteLater()
        self.app.processEvents()

    def test_card_and_cover_keep_stable_dimensions(self):
        self.assertEqual(self.card.size().width(), SearchResultCard.WIDTH)
        self.assertEqual(self.card.size().height(), SearchResultCard.HEIGHT)
        self.assertEqual(
            self.card.cover_label.height(),
            SearchResultCard.COVER_HEIGHT,
        )
        self.assertEqual(self.card.title_label.height(), 38)
        self.assertEqual(self.card.author_label.height(), 20)
        self.assertEqual(self.card.album_id_label.height(), 20)
        self.assertEqual(self.card.tags_label.height(), 20)
        self.assertFalse(self.card.favorite_visible)
        self.assertTrue(self.card.favorite_button.isHidden())

    def test_long_chinese_and_english_text_is_elided_with_full_tooltips(self):
        title = "这是一个非常长的中文漫画标题" * 8
        author = "UnbrokenEnglishAuthorName" * 12
        tags = ("超长标签内容" * 12, "AnotherUnbrokenTag" * 10)
        self.card.update_snapshot(
            make_snapshot(title=title, authors=(author,), tags=tags)
        )
        self.app.processEvents()

        self.assertEqual(self.card.title_label.toolTip(), safe_tooltip(title))
        self.assertEqual(
            self.card.author_label.toolTip(),
            safe_tooltip(f"作者：{author}"),
        )
        self.assertEqual(
            self.card.tags_label.toolTip(),
            safe_tooltip(f"标签：{' · '.join(tags)}"),
        )
        self.assertLessEqual(len(self.card.title_label.text().splitlines()), 2)
        self.assertIn("…", self.card.title_label.text())
        self.assertIn("…", self.card.author_label.text())
        self.assertIn("…", self.card.tags_label.text())

        for label in (
            self.card.title_label,
            self.card.author_label,
            self.card.album_id_label,
            self.card.tags_label,
        ):
            available = label.contentsRect().width()
            for line in label.text().splitlines():
                self.assertLessEqual(
                    label.fontMetrics().horizontalAdvance(line),
                    available,
                )

        self.assertEqual(self.card.size().width(), SearchResultCard.WIDTH)
        self.assertEqual(self.card.size().height(), SearchResultCard.HEIGHT)

    def test_missing_fields_use_neutral_display_text_without_mutating_snapshot(self):
        snapshot = make_snapshot(title=None, authors=(), tags=())
        self.card.update_snapshot(snapshot)
        self.app.processEvents()

        self.assertEqual(self.card.title_label.full_text, "JM 1449491")
        self.assertEqual(self.card.author_label.full_text, "作者：未知")
        self.assertEqual(self.card.album_id_label.full_text, "JM 1449491")
        self.assertEqual(self.card.tags_label.full_text, "")
        self.assertEqual(self.card.tags_label.text(), "")
        self.assertEqual(self.card.tags_label.height(), 20)
        self.assertIsNone(snapshot.title)
        self.assertEqual(snapshot.authors, ())
        self.assertEqual(snapshot.tags, ())

    def test_upstream_markup_like_text_is_rendered_as_plain_text(self):
        title = "<b>x</b>"
        self.card.update_snapshot(make_snapshot(title=title))
        self.app.processEvents()

        self.assertEqual(
            self.card.title_label.textFormat(),
            Qt.TextFormat.PlainText,
        )
        self.assertEqual(self.card.title_label.full_text, title)
        self.assertEqual(self.card.title_label.text(), title)
        self.assertEqual(
            self.card.title_label.toolTip(),
            "<qt>&lt;b&gt;x&lt;/b&gt;</qt>",
        )
        self.assertNotIn("<b>", self.card.title_label.toolTip())

    def test_cover_accepts_image_and_clear_restores_placeholder(self):
        image = QImage(320, 180, QImage.Format.Format_RGB32)
        image.fill(0xFF247A52)

        self.card.set_cover(image)
        pixmap = self.card.cover_label.pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertLessEqual(pixmap.width(), self.card.cover_label.width())
        self.assertLessEqual(pixmap.height(), self.card.cover_label.height())
        ratio_error = abs(
            pixmap.width() * image.height() - pixmap.height() * image.width()
        )
        self.assertLessEqual(ratio_error, max(image.width(), image.height()))
        self.assertEqual(self.card.cover_label.text(), "")

        self.card.clear_cover()
        self.assertTrue(self.card.cover_label.pixmap().isNull())
        self.assertEqual(self.card.cover_label.text(), "JM")

        self.card.set_cover(QImage())
        self.assertTrue(self.card.cover_label.pixmap().isNull())
        self.assertEqual(self.card.cover_label.text(), "JM")

    def test_action_switches_between_download_and_existing_task(self):
        downloads = []
        viewed = []
        self.card.download_requested.connect(downloads.append)
        self.card.view_task_requested.connect(viewed.append)

        self.assertFalse(self.card.task_present)
        self.assertEqual(self.card.action_button.text(), "下载")
        self.assertFalse(self.card.action_button.icon().isNull())
        self.card.action_button.click()
        self.assertEqual(downloads, ["1449491"])
        self.assertEqual(viewed, [])

        self.card.set_task_present(True)
        self.assertTrue(self.card.task_present)
        self.assertEqual(self.card.action_button.text(), "查看任务")
        self.assertFalse(self.card.action_button.icon().isNull())
        self.card.action_button.click()
        self.assertEqual(downloads, ["1449491"])
        self.assertEqual(viewed, ["1449491"])

        self.card.set_task_present(False)
        self.card.action_button.click()
        self.assertEqual(downloads, ["1449491", "1449491"])

    def test_favorite_action_is_explicit_stable_and_emits_only_when_available(self):
        requested = []
        self.card.favorite_requested.connect(requested.append)

        self.card.set_favorite_visible(True)
        self.card.set_favorite_state(available=False)
        self.app.processEvents()

        self.assertFalse(self.card.favorite_button.isHidden())
        self.assertEqual(self.card.favorite_button.size().width(), 32)
        self.assertEqual(self.card.favorite_button.size().height(), 32)
        self.assertFalse(self.card.favorite_button.isEnabled())
        self.card.favorite_button.click()
        self.assertEqual(requested, [])

        self.card.set_favorite_state(available=True)
        self.app.processEvents()
        self.assertTrue(self.card.favorite_button.isEnabled())
        gap = (
            self.card.action_button.geometry().left()
            - self.card.favorite_button.geometry().right()
            - 1
        )
        self.assertEqual(gap, 8)
        self.assertFalse(
            self.card.favorite_button.geometry().intersects(
                self.card.action_button.geometry()
            )
        )
        self.card.favorite_button.click()
        self.assertEqual(requested, ["1449491"])

        self.card.set_favorite_state(available=True, busy=True)
        self.assertTrue(self.card.favorite_busy)
        self.assertFalse(self.card.favorite_button.isEnabled())
        self.assertEqual(
            self.card.favorite_button.toolTip(),
            "正在添加到收藏",
        )

        self.card.set_favorite_state(available=True, favorited=True)
        self.assertTrue(self.card.favorited)
        self.assertTrue(self.card.favorite_button.isChecked())
        self.assertFalse(self.card.favorite_button.isEnabled())
        self.assertEqual(self.card.favorite_button.toolTip(), "已收藏")

    def test_move_action_is_separate_and_only_emits_when_available(self):
        moved = []
        self.card.move_favorite_requested.connect(moved.append)
        self.card.set_move_favorite_visible(True)
        self.card.move_button.click()
        self.assertEqual(moved, [])

        self.card.set_move_favorite_available(True)
        self.card.move_button.click()
        self.assertEqual(moved, ["1449491"])
        self.assertEqual(self.card.size().height(), SearchResultCard.HEIGHT)

    def test_checked_favorite_remains_visible_in_light_and_dark_themes(self):
        self.card.set_favorite_visible(True)
        self.card.set_favorite_state(available=True, favorited=True)
        previous_stylesheet = self.app.styleSheet()
        samples = {}
        try:
            for theme in (Theme.LIGHT, Theme.DARK):
                self.app.setStyleSheet(load_stylesheet(theme))
                self.app.processEvents()
                image = self.card.favorite_button.grab().toImage()
                samples[theme] = image.pixelColor(4, 4)
        finally:
            self.app.setStyleSheet(previous_stylesheet)
            self.app.processEvents()

        self.assertGreater(samples[Theme.LIGHT].lightness(), 180)
        self.assertLess(samples[Theme.DARK].lightness(), 90)
        self.assertNotEqual(samples[Theme.LIGHT], samples[Theme.DARK])
        self.assertTrue(self.card.favorite_button.isChecked())


if __name__ == "__main__":
    unittest.main()
