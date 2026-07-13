from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import TaskSnapshot, TaskStatus
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager


class FakeDownloadController(QObject):
    tasks_reset = Signal(object)
    command_failed = Signal(str, str)
    shutdown_finished = Signal(bool)

    def __init__(self, tasks=None):
        super().__init__()
        self.tasks = list(tasks or [])
        self.added = []
        self.retried = []
        self.removed = []
        self.opened = []
        self.shutdown_timeouts = []

    def list_tasks(self):
        return list(self.tasks)

    def add_task(self, album_id):
        self.added.append(album_id)
        snapshot = make_snapshot(
            task_id=f"task-{len(self.tasks) + 1}",
            album_id=album_id.strip().removeprefix("JM"),
            status=TaskStatus.FETCHING,
        )
        self.tasks.append(snapshot)
        self.tasks_reset.emit(self.list_tasks())
        return snapshot

    def retry_task(self, task_id):
        self.retried.append(task_id)

    def remove_task(self, task_id):
        self.removed.append(task_id)

    def open_item(self, album_id, kind):
        self.opened.append((album_id, kind))

    def has_active_tasks(self):
        return any(
            task.status
            in (TaskStatus.PENDING, TaskStatus.FETCHING, TaskStatus.DOWNLOADING)
            for task in self.tasks
        )

    def begin_shutdown(self, timeout=5.0):
        self.shutdown_timeouts.append(timeout)


def make_snapshot(
    task_id="task-1",
    album_id="123456",
    status=TaskStatus.PENDING,
    **changes,
):
    values = {
        "id": task_id,
        "album_id": album_id,
        "title": "测试漫画",
        "status": status,
        "progress": 0,
        "chapter": "",
        "page": "",
        "preview_path": None,
        "preview_revision": 0,
        "pdf_path": None,
        "error": None,
        "cover_url": None,
    }
    values.update(changes)
    return TaskSnapshot(**values)


class DownloadPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["download-page-tests"])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = QSettings(
            str(Path(self.temp_dir.name) / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        self.theme_manager = ThemeManager(settings)
        self.theme_manager.apply()
        self.controller = FakeDownloadController()
        self.window = MainWindow(self.theme_manager, self.controller)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.controller.tasks = []
        self.window._shutdown_complete = True
        self.window.close()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_search_fields_remain_placeholders_and_download_has_own_command(self):
        page = self.window.page("downloads")

        page.jm_id_search_input.setText("111")
        page.jm_id_search_input.returnPressed.emit()
        self.assertEqual(self.controller.added, [])

        page.view_tabs.setCurrentIndex(1)
        page.download_input.setText("JM123456")
        page.download_input.returnPressed.emit()
        self.app.processEvents()

        self.assertEqual(self.controller.added, ["JM123456"])
        self.assertEqual(page.download_input.text(), "")
        self.assertEqual(page.view_tabs.currentIndex(), 1)
        self.assertIn("task-1", page._task_rows)
        self.assertEqual(page.view_tabs.tabText(1), "下载任务 1")
        self.assertTrue(page.empty_tasks_label.isHidden())

        self.controller.tasks = []
        self.controller.tasks_reset.emit([])
        self.app.processEvents()
        self.assertFalse(page.empty_tasks_label.isHidden())
        self.assertEqual(page.view_tabs.tabText(1), "下载任务 0")

    def test_task_row_updates_actions_for_failed_and_completed_states(self):
        page = self.window.page("downloads")
        page.view_tabs.setCurrentIndex(1)
        active = make_snapshot(status=TaskStatus.DOWNLOADING, progress=42)
        self.controller.tasks = [active]
        self.controller.tasks_reset.emit([active])
        self.app.processEvents()
        row = page._task_rows[active.id]
        self.assertTrue(row.retry_button.isHidden())
        self.assertTrue(row.remove_button.isHidden())

        failed = replace(active, status=TaskStatus.FAILED, error="网络失败")
        self.controller.tasks = [failed]
        self.controller.tasks_reset.emit([failed])
        self.app.processEvents()
        self.assertFalse(row.retry_button.isHidden())
        self.assertFalse(row.remove_button.isHidden())
        self.assertTrue(row.progress.isHidden())
        row.retry_button.click()
        self.assertEqual(self.controller.retried, [active.id])

        completed = replace(
            active,
            status=TaskStatus.COMPLETED,
            progress=100,
            preview_path=Path(self.temp_dir.name) / "1.jpg",
            preview_revision=1,
            pdf_path=Path(self.temp_dir.name) / "1.pdf",
        )
        self.controller.tasks = [completed]
        self.controller.tasks_reset.emit([completed])
        self.app.processEvents()
        self.assertFalse(row.open_images_button.isHidden())
        self.assertFalse(row.open_pdf_button.isHidden())
        row.open_images_button.click()
        row.open_pdf_button.click()
        self.assertEqual(
            self.controller.opened,
            [(completed.album_id, "images"), (completed.album_id, "pdf")],
        )

    def test_close_with_active_task_requires_confirmation_and_waits_async(self):
        active = make_snapshot(status=TaskStatus.DOWNLOADING)
        self.controller.tasks = [active]

        with patch(
            "jm_downloader.qt.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.controller.shutdown_timeouts, [])

        with patch(
            "jm_downloader.qt.main_window.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.assertFalse(self.window.close())
        self.assertEqual(self.controller.shutdown_timeouts, [5.0])
        self.assertFalse(self.window.isEnabled())

        self.controller.shutdown_finished.emit(True)
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
