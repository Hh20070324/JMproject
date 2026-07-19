from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, QTimer, Signal
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
        self.paused = []
        self.resumed = []
        self.cancelled = []
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

    def pause_task(self, task_id):
        self.paused.append(task_id)

    def resume_task(self, task_id):
        self.resumed.append(task_id)

    def cancel_task(self, task_id, delete_files=False):
        self.cancelled.append((task_id, delete_files))

    def remove_task(self, task_id):
        self.removed.append(task_id)

    def open_item(self, album_id, kind):
        self.opened.append((album_id, kind))

    def open_task_item(self, task_id, kind):
        self.opened.append((task_id, kind))

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
        self.theme_manager = ThemeManager()
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

    def test_manual_download_stays_separate_when_search_is_unavailable(self):
        page = self.window.page("downloads")

        page.jm_id_search_input.setText("111")
        page.jm_id_search_input.returnPressed.emit()
        self.assertEqual(self.controller.added, [])

        page.view_tabs.setCurrentIndex(1)
        page.download_input.setText("JM123456")
        page.download_input.returnPressed.emit()
        self.app.processEvents()

        self.assertEqual(self.controller.added, ["123456"])
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
        self.assertFalse(row.pause_button.isHidden())
        self.assertFalse(row.cancel_button.isHidden())
        row.pause_button.click()
        self.assertEqual(self.controller.paused, [active.id])

        failed = replace(active, status=TaskStatus.FAILED, error="网络失败")
        self.controller.tasks = [failed]
        self.controller.tasks_reset.emit([failed])
        self.app.processEvents()
        self.assertFalse(row.retry_button.isHidden())
        self.assertTrue(row.remove_button.isHidden())
        self.assertFalse(row.cancel_button.isHidden())
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
            [(completed.id, "images"), (completed.id, "pdf")],
        )

    def test_task_row_displays_transitional_states_without_actions(self):
        page = self.window.page("downloads")
        page.view_tabs.setCurrentIndex(1)
        pausing = make_snapshot(status=TaskStatus.PAUSING, progress=42)
        self.controller.tasks = [pausing]
        self.controller.tasks_reset.emit([pausing])
        self.app.processEvents()
        row = page._task_rows[pausing.id]

        self.assertEqual(row.status.text(), "暂停中")
        self.assertIn("等待当前下载请求结束", row.detail.toolTip())
        self.assertTrue(row.retry_button.isHidden())
        self.assertTrue(row.remove_button.isHidden())

        cancelling = replace(pausing, status=TaskStatus.CANCELLING)
        self.controller.tasks = [cancelling]
        self.controller.tasks_reset.emit([cancelling])
        self.app.processEvents()
        self.assertEqual(row.status.text(), "取消中")
        self.assertIn("安全停止", row.detail.toolTip())

    def test_cancel_dialog_routes_keep_delete_and_back_choices(self):
        page = self.window.page("downloads")
        paused = make_snapshot(status=TaskStatus.PAUSED)
        self.controller.tasks = [paused]
        self.controller.tasks_reset.emit([paused])
        self.app.processEvents()

        class FakeMessageBox:
            Icon = QMessageBox.Icon
            ButtonRole = QMessageBox.ButtonRole
            StandardButton = QMessageBox.StandardButton
            selection = ""
            warning_answer = QMessageBox.StandardButton.No
            warning_calls = []

            def __init__(self, _parent):
                self.buttons = {}

            def setIcon(self, _icon):
                pass

            def setWindowTitle(self, _title):
                pass

            def setText(self, _text):
                pass

            def setInformativeText(self, _text):
                pass

            def addButton(self, label, _role):
                button = object()
                self.buttons[label] = button
                return button

            def setDefaultButton(self, _button):
                pass

            def setEscapeButton(self, _button):
                pass

            def exec(self):
                pass

            def clickedButton(self):
                return self.buttons.get(self.selection)

            @classmethod
            def warning(cls, *args):
                cls.warning_calls.append(args)
                return cls.warning_answer

        with patch(
            "jm_downloader.qt.pages.download_page.QMessageBox",
            FakeMessageBox,
        ):
            FakeMessageBox.selection = "仅移除任务"
            page._confirm_cancel(paused.id)
            FakeMessageBox.selection = "移除并删除文件"
            FakeMessageBox.warning_answer = QMessageBox.StandardButton.No
            page._confirm_cancel(paused.id)
            FakeMessageBox.warning_answer = QMessageBox.StandardButton.Yes
            page._confirm_cancel(paused.id)
            FakeMessageBox.selection = "返回"
            page._confirm_cancel(paused.id)

        self.assertEqual(
            self.controller.cancelled,
            [(paused.id, False), (paused.id, True)],
        )
        warning_text = FakeMessageBox.warning_calls[-1][2]
        self.assertIn("全部章节图片", warning_text)
        self.assertIn("PDF", warning_text)
        self.assertIn("本次任务开始前", warning_text)

    def test_completed_task_is_scheduled_for_removal_after_five_seconds(self):
        page = self.window.page("downloads")
        completed = make_snapshot(status=TaskStatus.COMPLETED, progress=100)

        with patch.object(QTimer, "singleShot") as single_shot:
            self.controller.tasks = [completed]
            self.controller.tasks_reset.emit([completed])
            self.app.processEvents()

        delay, callback = single_shot.call_args.args
        self.assertEqual(delay, 5000)
        callback()
        self.assertEqual(self.controller.removed, [completed.id])

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
