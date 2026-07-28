import os
from pathlib import Path
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ReaderPageSnapshot,
    ReaderPageState,
    TaskSnapshot,
    TaskStatus,
)
from jm_downloader.qt.controllers.download_controller import (
    BatchCommandOutcome,
)
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.pages.download_page import DownloadPage
from jm_downloader.qt.reader_window import ReaderWindow
from jm_downloader.qt.theme import Theme, load_stylesheet
from jm_downloader.qt.widgets.reader_graphics_view import ReaderGraphicsView
from jm_downloader.settings import READER_ZOOM_LEVELS


def reader_pages(count=2_000):
    return tuple(
        ReaderPageSnapshot(
            "301",
            page,
            count,
            ReaderPageState.PLACEHOLDER,
        )
        for page in range(1, count + 1)
    )


def task_snapshot(task_id, status):
    numeric_id = str(int(task_id.removeprefix("task-")) + 1)
    return TaskSnapshot(
        id=task_id,
        album_id=numeric_id,
        title=f"规模任务 {numeric_id}",
        status=status,
        progress=0,
        chapter="",
        page="",
        preview_path=None,
        preview_revision=0,
        pdf_directory=None,
        error="测试失败" if status is TaskStatus.FAILED else None,
        cover_url=None,
    )


class IdleReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError("reliability test stays offline")

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError("reliability test stays offline")

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError("reliability test stays offline")

    async def close(self):
        return True


class ScaleBatchController(QObject):
    tasks_reset = Signal(object)
    command_failed = Signal(str, str)

    def __init__(self, snapshots):
        super().__init__()
        self.tasks = list(snapshots)

    def list_tasks(self):
        return list(self.tasks)

    def publish(self, snapshots):
        self.tasks = list(snapshots)
        self.tasks_reset.emit(self.list_tasks())

    @staticmethod
    def _accepted(command, task_ids):
        return BatchCommandOutcome(
            command,
            accepted_ids=tuple(task_ids),
        )

    def batch_pause(self, task_ids):
        return self._accepted("pause", task_ids)

    def batch_resume(self, task_ids):
        return self._accepted("resume", task_ids)

    def batch_cancel(self, task_ids):
        return self._accepted("cancel", task_ids)

    def pause_task(self, _task_id):
        return None

    def resume_task(self, _task_id):
        return None

    def cancel_task(self, _task_id, _delete_files=False):
        return None

    def retry_task(self, _task_id):
        return None

    def remove_task(self, _task_id):
        return None

    def open_task_item(self, _task_id, _kind):
        return None

    def add_task(self, _album_id):
        return None


class V31ReaderScaleReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-scale-reliability-tests"]
        )

    def test_two_thousand_pages_handle_all_zoom_levels_and_rapid_jumps(self):
        view = ReaderGraphicsView()
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.resize(900, 700)
        view.show()
        view.set_pages(reader_pages())
        self.app.processEvents()

        started = time.monotonic()
        for mode in ("fit_width", "fit_page"):
            view.set_layout_mode(mode)
            for percent in sorted(READER_ZOOM_LEVELS):
                view.set_zoom_percent(percent)
                for page in (1, 2_000, 997, 17):
                    view.scroll_to_page(page)
                self.app.processEvents()
                self.assertLessEqual(view.target_width, 4096)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0)
        self.assertEqual(view.loaded_image_bytes, 0)
        self.assertEqual(len(view._pages), 2_000)
        view.close()
        view.deleteLater()
        self.app.processEvents()

    def test_one_reader_window_survives_repeated_open_close_cycles(self):
        controller = ReaderController(
            IdleReaderService(),
            result_interval_ms=5,
        )
        window = ReaderWindow(
            controller,
            persist_geometry=False,
        )
        window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        initial_generation = controller.generation
        for index in range(20):
            window.begin_session(
                str(100 + index),
                f"漫画 {index}",
            )
            self.app.processEvents()
            self.assertTrue(window.isVisible())
            self.assertFalse(window.isModal())
            window.close()
            self.app.processEvents()
            self.assertFalse(window.has_session)
            self.assertIsNone(window.session_album_id)

        self.assertGreater(controller.generation, initial_generation)
        window.deleteLater()
        controller.shutdown(timeout=2.0)
        controller.deleteLater()
        self.app.processEvents()


class V31BatchScaleReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-batch-scale-reliability-tests"]
        )

    def setUp(self):
        statuses = (
            TaskStatus.PENDING,
            TaskStatus.PAUSED,
            TaskStatus.FAILED,
            TaskStatus.PAUSING,
            TaskStatus.CANCELLING,
            TaskStatus.COMPLETED,
        )
        self.controller = ScaleBatchController(
            tuple(
                task_snapshot(
                    f"task-{index}",
                    statuses[index % len(statuses)],
                )
                for index in range(200)
            )
        )
        self.page = DownloadPage(self.controller)
        self.page.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.page.show()
        self.page.view_tabs.setCurrentIndex(1)
        self.page.batch_manage_button.click()
        self.app.processEvents()

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()

    def test_two_hundred_tasks_prune_selection_during_transition_and_removal(self):
        self.page.batch_select_all_button.click()
        expected = {
            snapshot.id
            for snapshot in self.controller.tasks
            if snapshot.status
            in {
                TaskStatus.PENDING,
                TaskStatus.PAUSED,
                TaskStatus.FAILED,
            }
        }
        self.assertEqual(self.page._batch_selected_ids, expected)

        remaining = tuple(
            task_snapshot(f"task-{index}", TaskStatus.COMPLETED)
            for index in range(25)
        )
        self.controller.publish(remaining)
        self.app.processEvents()

        self.assertEqual(self.page._batch_selected_ids, set())
        self.assertEqual(len(self.page._task_rows), 25)
        self.assertEqual(
            self.page.batch_selected_label.text(),
            "已选 0 个任务",
        )

    def test_batch_bar_does_not_overlap_at_four_effective_scale_widths(self):
        previous_stylesheet = self.app.styleSheet()
        try:
            for theme in (Theme.LIGHT, Theme.DARK):
                self.app.setStyleSheet(load_stylesheet(theme))
                for scale in (1.0, 1.25, 1.5, 2.0):
                    with self.subTest(theme=theme.value, scale=scale):
                        logical_width = max(540, round(1_100 / scale))
                        logical_height = max(520, round(720 / scale))
                        self.page.resize(logical_width, logical_height)
                        self.app.processEvents()
                        buttons = (
                            self.page.batch_select_all_button,
                            self.page.batch_pause_button,
                            self.page.batch_resume_button,
                            self.page.batch_cancel_button,
                            self.page.batch_exit_button,
                        )
                        visible = [
                            button for button in buttons
                            if button.isVisible()
                        ]
                        for index, button in enumerate(visible):
                            self.assertGreater(button.width(), 0)
                            self.assertGreaterEqual(button.geometry().left(), 0)
                            self.assertLessEqual(
                                button.geometry().right(),
                                self.page.batch_selection_bar.width(),
                            )
                            for other in visible[index + 1:]:
                                self.assertFalse(
                                    button.geometry().intersects(
                                        other.geometry()
                                    )
                                )
        finally:
            self.app.setStyleSheet(previous_stylesheet)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
