import os
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ReaderPageSnapshot,
    ReaderPageState,
)
from jm_downloader.qt.widgets.reader_graphics_view import (
    MAX_DECODE_TARGET_WIDTH,
    PAGE_GAP,
    ReaderGraphicsView,
)


def pages(count, *, width=801, height=1201):
    return tuple(
        ReaderPageSnapshot(
            "301",
            number,
            count,
            ReaderPageState.READY,
            width=width,
            height=height,
        )
        for number in range(1, count + 1)
    )


class ReaderRenderingQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = ReaderGraphicsView()
        self.view.resize(1000, 720)
        self.view.show()
        self.app.processEvents()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def test_shrink_zoom_keeps_display_scale_but_decodes_at_full_baseline(self):
        baseline = self.view._base_width
        targets = {}
        displays = {}
        for percent in (25, 50, 75, 100, 125, 150):
            self.view.set_zoom_percent(percent)
            targets[percent] = self.view.target_width
            displays[percent] = self.view.content_width

        self.assertEqual(displays[25], round(baseline * 0.25))
        self.assertEqual(displays[50], round(baseline * 0.50))
        self.assertEqual(displays[75], round(baseline * 0.75))
        self.assertEqual(targets[25], min(baseline, MAX_DECODE_TARGET_WIDTH))
        self.assertEqual(targets[50], min(baseline, MAX_DECODE_TARGET_WIDTH))
        self.assertEqual(targets[75], min(baseline, MAX_DECODE_TARGET_WIDTH))
        self.assertGreater(targets[125], targets[100])
        self.assertLessEqual(targets[150], MAX_DECODE_TARGET_WIDTH)

    def test_fit_width_uses_integer_contiguous_page_boundaries(self):
        self.assertEqual(PAGE_GAP, 0)
        self.view.set_pages(pages(3))

        for percent in (25, 50, 100):
            self.view.set_zoom_percent(percent)
            for index, page in enumerate(self.view._pages, start=1):
                self.assertEqual(page.slot_height, int(page.slot_height))
                self.assertEqual(
                    self.view.page_top(index),
                    int(self.view.page_top(index)),
                )
            self.assertEqual(
                self.view.page_top(2),
                self.view._pages[0].slot_height,
            )
            self.assertEqual(
                self.view.page_top(3),
                self.view._pages[0].slot_height
                + self.view._pages[1].slot_height,
            )

    def test_cross_page_black_image_has_no_background_seam(self):
        source = QImage(801, 1201, QImage.Format.Format_RGB32)
        source.fill(QColor("black"))
        self.view.set_pages(pages(2))
        for number in (1, 2):
            self.view.set_page_ready(
                ReaderPageSnapshot(
                    "301",
                    number,
                    2,
                    ReaderPageState.READY,
                    width=801,
                    height=1201,
                ),
                source,
            )

        for percent in (25, 50, 100):
            self.view.set_zoom_percent(percent)
            scene_rect = self.view.scene().sceneRect()
            canvas = QImage(
                max(1, int(round(scene_rect.width()))),
                max(1, int(round(scene_rect.height()))),
                QImage.Format.Format_RGB32,
            )
            canvas.fill(QColor("magenta"))
            painter = QPainter(canvas)
            self.view.scene().render(
                painter,
                QRectF(canvas.rect()),
                scene_rect,
            )
            painter.end()
            boundary = int(self.view.page_top(2))
            x = int(round(scene_rect.width() / 2))
            self.assertEqual(canvas.pixelColor(x, boundary - 1), QColor("black"))
            self.assertEqual(canvas.pixelColor(x, boundary), QColor("black"))

    def test_fit_page_keeps_slot_semantics_separate(self):
        self.view.set_pages(pages(2))
        self.view.set_layout_mode("fit_page")
        self.view.set_zoom_percent(50)

        self.assertEqual(
            self.view.page_top(2),
            self.view._pages[0].slot_height,
        )
        self.assertGreaterEqual(
            self.view._pages[0].slot_height,
            self.view._pages[0].display_height,
        )


if __name__ == "__main__":
    unittest.main()
