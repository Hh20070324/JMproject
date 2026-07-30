import os
import unittest

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from jm_downloader.models import SearchResultSnapshot
from jm_downloader.qt.theme import Theme, load_stylesheet
from jm_downloader.qt.widgets.search_result_card import SearchResultCard


class V322ReadButtonUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v322-card-ui-tests"]
        )

    def setUp(self):
        self.card = SearchResultCard(
            SearchResultSnapshot("123", "测试漫画")
        )
        self.card.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.card.set_reading_available(True)
        self.card.show()
        self.app.processEvents()

    def tearDown(self):
        self.card.close()
        self.card.deleteLater()
        self.app.setStyleSheet("")
        self.app.processEvents()

    def test_both_themes_define_normal_hover_pressed_focus_and_disabled(self):
        for theme in (Theme.LIGHT, Theme.DARK):
            stylesheet = load_stylesheet(theme)
            self.assertIn(
                "QPushButton#searchResultReadButton {",
                stylesheet,
            )
            for state in ("hover", "pressed", "focus", "disabled"):
                self.assertIn(
                    f"QPushButton#searchResultReadButton:{state}",
                    stylesheet,
                )

    def test_read_and_checking_copy_fit_at_four_supported_scales(self):
        for theme in (Theme.LIGHT, Theme.DARK):
            self.app.setStyleSheet(load_stylesheet(theme))
            for point_size in (9, 11, 14, 18):
                font = QFont(self.card.font())
                font.setPointSize(point_size)
                self.card.setFont(font)
                self.card.read_button.setFont(font)
                self.card.set_reading_checking(False)
                self.app.processEvents()

                self.assertLessEqual(
                    self.card.read_button.fontMetrics().horizontalAdvance(
                        self.card.read_button.text()
                    ),
                    self.card.read_button.contentsRect().width(),
                )
                self.card.set_reading_checking(True)
                self.app.processEvents()
                self.assertLessEqual(
                    self.card.read_button.fontMetrics().horizontalAdvance(
                        self.card.read_button.text()
                    ),
                    self.card.read_button.contentsRect().width(),
                )
                self.assertFalse(
                    self.card.read_button.geometry().intersects(
                        self.card.action_button.geometry()
                    )
                )

    def test_keyboard_activation_submits_once_and_busy_blocks_reactivation(self):
        requests = []
        self.card.read_requested.connect(requests.append)
        self.card.read_button.setFocus()

        QTest.keyClick(self.card.read_button, Qt.Key.Key_Space)
        self.card.set_reading_checking(True)
        QTest.keyClick(self.card.read_button, Qt.Key.Key_Space)

        self.assertEqual(requests, ["123"])


if __name__ == "__main__":
    unittest.main()
