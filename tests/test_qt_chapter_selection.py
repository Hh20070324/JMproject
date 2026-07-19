from html import escape
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    SearchResultSnapshot,
)
from jm_downloader.qt.widgets.chapter_selection_dialog import (
    ChapterSelectionDialog,
)
from jm_downloader.qt.widgets.search_result_card import SearchResultCard


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_catalog(*chapters: ChapterSnapshot) -> ChapterCatalogSnapshot:
    if not chapters:
        chapters = (
            ChapterSnapshot("photo-first", 7, "开场"),
            ChapterSnapshot("photo-second", 2, "中篇"),
            ChapterSnapshot("photo-third", 11, "终章"),
        )
    return ChapterCatalogSnapshot(
        album_id="1449491",
        title="测试漫画",
        chapters=tuple(chapters),
    )


def make_search_result() -> SearchResultSnapshot:
    return SearchResultSnapshot(
        album_id="1449491",
        title="测试漫画",
        authors=("测试作者",),
        tags=("全彩",),
        chapter_catalog=None,
    )


def safe_tooltip(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return f"<qt>{escape(normalized, quote=True).replace(chr(10), '<br>')}</qt>"


class ChapterSelectionDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["chapter-selection-dialog-tests"]
        )

    def setUp(self):
        self.catalog = make_catalog()
        self.dialog = ChapterSelectionDialog(self.catalog)
        self.dialog.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.dialog.resize(520, 480)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_preserves_official_order_and_defaults_to_only_first_chapter(self):
        boxes = tuple(self.dialog.chapter_checkboxes)

        self.assertEqual(
            [box.property("chapter_id") for box in boxes],
            ["photo-first", "photo-second", "photo-third"],
        )
        self.assertEqual(
            [box.full_text for box in boxes],
            [
                "第 7 章 · 开场",
                "第 2 章 · 中篇",
                "第 11 章 · 终章",
            ],
        )
        self.assertEqual(
            [box.isChecked() for box in boxes],
            [True, False, False],
        )
        self.assertEqual(
            self.dialog.selected_chapter_ids(),
            ("photo-first",),
        )
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        self.assertIn("1", self.dialog.confirm_button.text())

    def test_select_all_is_three_state_and_tracks_individual_choices(self):
        select_all = self.dialog.select_all_checkbox
        boxes = tuple(self.dialog.chapter_checkboxes)

        self.assertEqual(select_all.text(), "全选")
        self.assertTrue(select_all.isTristate())
        self.assertEqual(
            select_all.checkState(),
            Qt.CheckState.PartiallyChecked,
        )

        select_all.click()
        self.app.processEvents()
        self.assertEqual(select_all.checkState(), Qt.CheckState.Checked)
        self.assertTrue(all(box.isChecked() for box in boxes))
        self.assertEqual(
            self.dialog.selected_chapter_ids(),
            ("photo-first", "photo-second", "photo-third"),
        )
        self.assertIn("3", self.dialog.confirm_button.text())

        boxes[1].click()
        self.app.processEvents()
        self.assertEqual(
            select_all.checkState(),
            Qt.CheckState.PartiallyChecked,
        )
        self.assertEqual(
            self.dialog.selected_chapter_ids(),
            ("photo-first", "photo-third"),
        )

        boxes[0].click()
        boxes[2].click()
        self.app.processEvents()
        self.assertEqual(select_all.checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(self.dialog.selected_chapter_ids(), ())

    def test_select_all_single_click_toggles_all_then_none_from_partial(self):
        select_all = self.dialog.select_all_checkbox
        boxes = tuple(self.dialog.chapter_checkboxes)

        self.assertEqual(
            select_all.checkState(),
            Qt.CheckState.PartiallyChecked,
        )
        select_all.click()
        self.app.processEvents()
        self.assertTrue(all(box.isChecked() for box in boxes))
        self.assertEqual(select_all.checkState(), Qt.CheckState.Checked)

        select_all.click()
        self.app.processEvents()
        self.assertTrue(all(not box.isChecked() for box in boxes))
        self.assertEqual(select_all.checkState(), Qt.CheckState.Unchecked)

        select_all.click()
        self.app.processEvents()
        self.assertTrue(all(box.isChecked() for box in boxes))
        self.assertEqual(select_all.checkState(), Qt.CheckState.Checked)

    def test_chapter_hover_state_tracks_enter_and_leave_in_both_directions(self):
        first, second, third = self.dialog.chapter_checkboxes

        for source, target in ((second, first), (second, third)):
            QApplication.sendEvent(source, QEvent(QEvent.Type.Enter))
            QApplication.sendEvent(source, QEvent(QEvent.Type.Leave))
            QApplication.sendEvent(target, QEvent(QEvent.Type.Enter))
            self.app.processEvents()

            self.assertFalse(source.property("hovered"))
            self.assertTrue(target.property("hovered"))

            QApplication.sendEvent(target, QEvent(QEvent.Type.Leave))
            self.assertFalse(target.property("hovered"))

    def test_confirm_is_disabled_until_at_least_one_chapter_is_selected(self):
        first, second, _third = self.dialog.chapter_checkboxes

        first.click()
        self.app.processEvents()
        self.assertEqual(self.dialog.selected_chapter_ids(), ())
        self.assertFalse(self.dialog.confirm_button.isEnabled())
        self.assertIn("0", self.dialog.confirm_button.text())

        second.click()
        self.app.processEvents()
        self.assertEqual(
            self.dialog.selected_chapter_ids(),
            ("photo-second",),
        )
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        self.assertIn("1", self.dialog.confirm_button.text())

    def test_non_contiguous_selection_is_returned_in_official_order(self):
        first, second, third = self.dialog.chapter_checkboxes

        second.click()
        third.click()
        self.app.processEvents()

        self.assertTrue(first.isChecked())
        self.assertTrue(third.isChecked())
        self.assertEqual(
            self.dialog.selected_chapter_ids(),
            ("photo-first", "photo-second", "photo-third"),
        )

        second.click()
        self.app.processEvents()
        self.assertEqual(
            self.dialog.selected_chapter_ids(),
            ("photo-first", "photo-third"),
        )

    def test_long_markup_like_title_is_elided_with_safe_full_tooltip(self):
        title = "<b>不是粗体</b>" + "非常长的章节标题" * 24
        dialog = ChapterSelectionDialog(
            make_catalog(ChapterSnapshot("photo-long", 12, title))
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.resize(420, 320)
        dialog.show()
        self.app.processEvents()
        try:
            box = dialog.chapter_checkboxes[0]
            full_text = f"第 12 章 · {title}"
            self.assertEqual(box.property("chapter_id"), "photo-long")
            self.assertEqual(box.full_text, full_text)
            self.assertEqual(box.toolTip(), safe_tooltip(full_text))
            self.assertNotIn("<b>", box.toolTip())
            self.assertIn("<b>", box.text())
            self.assertIn("…", box.text())
            self.assertLessEqual(
                box.fontMetrics().horizontalAdvance(box.text()),
                box.contentsRect().width(),
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_layout_remains_stable_at_supported_scale_factors(self):
        script = textwrap.dedent(
            """
            from PySide6.QtCore import QPoint, QRect, Qt
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtWidgets import QApplication, QScrollArea

            from jm_downloader.models import ChapterCatalogSnapshot, ChapterSnapshot
            from jm_downloader.qt.widgets.chapter_selection_dialog import ChapterSelectionDialog

            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
            app = QApplication(["chapter-selection-scale-audit"])
            catalog = ChapterCatalogSnapshot(
                "1449491",
                "缩放测试",
                tuple(
                    ChapterSnapshot(
                        f"photo-{index}",
                        index,
                        ("第一个带有很长标题的章节" * 12) if index == 1 else f"章节 {index}",
                    )
                    for index in range(1, 31)
                ),
            )
            dialog = ChapterSelectionDialog(catalog)
            dialog.resize(520, 480)
            dialog.show()
            app.processEvents()

            scroll_areas = dialog.findChildren(QScrollArea)
            assert len(scroll_areas) == 1
            scroll = scroll_areas[0]
            assert not scroll.horizontalScrollBar().isVisible()
            assert dialog.select_all_checkbox.isVisible()
            assert dialog.confirm_button.isVisible()

            def mapped_rect(widget):
                return QRect(widget.mapTo(dialog, QPoint(0, 0)), widget.size())

            select_all_rect = mapped_rect(dialog.select_all_checkbox)
            confirm_rect = mapped_rect(dialog.confirm_button)
            assert dialog.contentsRect().contains(select_all_rect)
            assert dialog.contentsRect().contains(confirm_rect)
            assert not select_all_rect.intersects(confirm_rect)

            for box in dialog.chapter_checkboxes:
                assert box.width() <= scroll.viewport().width()
                assert box.fontMetrics().horizontalAdvance(box.text()) <= box.contentsRect().width()

            image = dialog.grab().toImage()
            assert not image.isNull()
            assert image.width() >= dialog.width()
            assert image.height() >= dialog.height()
            dialog.close()
            app.processEvents()
            """
        )
        for factor in ("1", "1.25", "1.5", "2"):
            with self.subTest(scale_factor=factor):
                environment = os.environ.copy()
                environment["QT_QPA_PLATFORM"] = "offscreen"
                environment["QT_SCALE_FACTOR"] = factor
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=(
                        f"scale={factor}\nstdout:\n{completed.stdout}"
                        f"\nstderr:\n{completed.stderr}"
                    ),
                )


class SearchResultCardChapterStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["search-result-card-chapter-state-tests"]
        )

    def setUp(self):
        self.card = SearchResultCard(make_search_result())
        self.card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.card.show()
        self.app.processEvents()

    def tearDown(self):
        self.card.close()
        self.card.deleteLater()
        self.app.processEvents()

    def test_unknown_loading_single_and_multiple_states_keep_fixed_geometry(self):
        card_size = self.card.size()
        button_geometry = self.card.action_button.geometry()

        self.assertEqual(self.card.action_button.text(), "下载")
        self.assertTrue(self.card.action_button.isEnabled())

        self.card.set_chapter_state(loading=True)
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "读取章节…")
        self.assertFalse(self.card.action_button.isEnabled())
        self.assertEqual(self.card.size(), card_size)
        self.assertEqual(self.card.action_button.geometry(), button_geometry)

        single = make_catalog(ChapterSnapshot("photo-only", 1, "单话"))
        self.card.set_chapter_state(single)
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "下载整本")
        self.assertTrue(self.card.action_button.isEnabled())
        self.assertEqual(self.card.size(), card_size)
        self.assertEqual(self.card.action_button.geometry(), button_geometry)

        self.card.set_chapter_state(make_catalog())
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "章节选择")
        self.assertTrue(self.card.action_button.isEnabled())
        self.assertEqual(self.card.size(), card_size)
        self.assertEqual(self.card.action_button.geometry(), button_geometry)

        self.card.set_chapter_state()
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "下载")
        self.assertTrue(self.card.action_button.isEnabled())
        self.assertEqual(self.card.size(), card_size)
        self.assertEqual(self.card.action_button.geometry(), button_geometry)

    def test_existing_task_takes_priority_and_restores_latest_chapter_state(self):
        multi = make_catalog()
        viewed = []
        downloads = []
        self.card.view_task_requested.connect(viewed.append)
        self.card.download_requested.connect(downloads.append)

        self.card.set_chapter_state(loading=True)
        self.card.set_task_present(True)
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "查看任务")
        self.assertTrue(self.card.action_button.isEnabled())

        self.card.set_chapter_state(multi)
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "查看任务")
        self.assertTrue(self.card.action_button.isEnabled())
        self.card.action_button.click()
        self.assertEqual(viewed, ["1449491"])
        self.assertEqual(downloads, [])

        self.card.set_task_present(False)
        self.app.processEvents()
        self.assertEqual(self.card.action_button.text(), "章节选择")
        self.card.action_button.click()
        self.assertEqual(downloads, ["1449491"])

    def test_loading_disables_action_without_changing_non_task_signal_contract(self):
        requested = []
        self.card.download_requested.connect(requested.append)

        self.card.action_button.click()
        self.card.set_chapter_state(loading=True)
        self.card.action_button.click()
        self.card.set_chapter_state(
            make_catalog(ChapterSnapshot("photo-only", 1, "单话"))
        )
        self.card.action_button.click()
        self.card.set_chapter_state(make_catalog())
        self.card.action_button.click()

        self.assertEqual(
            requested,
            ["1449491", "1449491", "1449491"],
        )


if __name__ == "__main__":
    unittest.main()
