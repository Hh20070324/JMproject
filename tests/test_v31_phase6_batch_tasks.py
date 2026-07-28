from dataclasses import FrozenInstanceError, replace
import os
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from jm_downloader.models import TaskSnapshot, TaskStatus
from jm_downloader.qt.controllers.download_controller import (
    BatchCommandOutcome,
    DownloadController,
)
from jm_downloader.qt.pages.download_page import DownloadPage
from jm_downloader.qt.theme import Theme, load_stylesheet
from jm_downloader.tasks import InvalidTaskState, TaskNotFound


def task_snapshot(
    task_id: str,
    status: TaskStatus,
) -> TaskSnapshot:
    return TaskSnapshot(
        id=task_id,
        album_id=task_id.removeprefix("task-") or "1",
        title=f"任务 {task_id}",
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


class FakeBatchManager:
    def __init__(self, snapshots):
        self.tasks = {
            snapshot.id: snapshot for snapshot in snapshots
        }
        self.calls = []
        self.failures = set()
        self.task_store = SimpleNamespace(last_error=None)
        self.listener = queue.Queue()

    def add_listener(self):
        return self.listener

    def remove_listener(self, listener):
        self.assert_listener(listener)

    def assert_listener(self, listener):
        if listener is not self.listener:
            raise AssertionError("unexpected listener")

    def list_tasks(self):
        return list(self.tasks.values())

    def get_task(self, task_id):
        try:
            return self.tasks[task_id]
        except KeyError as error:
            raise TaskNotFound("未找到该任务") from error

    def pause(self, task_id):
        self._apply(task_id, "pause", TaskStatus.PAUSED)

    def resume(self, task_id):
        self._apply(task_id, "resume", TaskStatus.PENDING)

    def cancel(self, task_id):
        self._apply(task_id, "cancel", TaskStatus.CANCELLING)

    def _apply(self, task_id, command, status):
        if (command, task_id) in self.failures:
            raise InvalidTaskState("任务状态刚刚发生变化")
        self.calls.append((command, task_id))
        self.tasks[task_id] = replace(
            self.get_task(task_id),
            status=status,
        )

    def restore_preview(self, _task_id):
        return None


class FakeBatchController(QObject):
    tasks_reset = Signal(object)
    command_failed = Signal(str, str)

    def __init__(self, snapshots):
        super().__init__()
        self.tasks = list(snapshots)
        self.batch_calls = []

    def list_tasks(self):
        return list(self.tasks)

    def publish(self, snapshots):
        self.tasks = list(snapshots)
        self.tasks_reset.emit(self.list_tasks())

    def batch_pause(self, task_ids):
        values = tuple(task_ids)
        self.batch_calls.append(("pause", values))
        return BatchCommandOutcome(
            "pause",
            accepted_ids=values[:1],
            skipped_ids=values[1:],
        )

    def batch_resume(self, task_ids):
        values = tuple(task_ids)
        self.batch_calls.append(("resume", values))
        return BatchCommandOutcome(
            "resume",
            accepted_ids=values,
        )

    def batch_cancel(self, task_ids):
        values = tuple(task_ids)
        self.batch_calls.append(("cancel", values))
        return BatchCommandOutcome(
            "cancel",
            accepted_ids=values,
        )

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


class BatchCommandControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-batch-controller-tests"]
        )

    def make_controller(self, snapshots):
        manager = FakeBatchManager(snapshots)
        controller = DownloadController(
            manager,
            SimpleNamespace(),
            event_interval_ms=10000,
            reconcile_interval_ms=10000,
        )
        self.addCleanup(self.app.processEvents)
        self.addCleanup(controller.dispose)
        self.addCleanup(controller.deleteLater)
        return manager, controller

    def test_pause_uses_execution_state_and_returns_one_immutable_summary(self):
        manager, controller = self.make_controller(
            (
                task_snapshot("task-1", TaskStatus.PENDING),
                task_snapshot("task-2", TaskStatus.DOWNLOADING),
                task_snapshot("task-3", TaskStatus.PAUSED),
                task_snapshot("task-4", TaskStatus.COMPLETED),
            )
        )
        manager.failures.add(("pause", "task-2"))
        command_errors = []
        controller.command_failed.connect(command_errors.append)

        outcome = controller.batch_pause(
            ("task-1", "task-2", "task-3", "task-4", "missing")
        )

        self.assertEqual(outcome.accepted_ids, ("task-1",))
        self.assertEqual(outcome.skipped_ids, ("task-3", "task-4"))
        self.assertEqual(
            tuple(failure.task_id for failure in outcome.failures),
            ("task-2", "missing"),
        )
        self.assertEqual(manager.calls, [("pause", "task-1")])
        self.assertEqual(command_errors, [])
        with self.assertRaises(FrozenInstanceError):
            outcome.command = "cancel"

    def test_resume_never_turns_failed_tasks_into_retries(self):
        manager, controller = self.make_controller(
            (
                task_snapshot("task-1", TaskStatus.PAUSED),
                task_snapshot("task-2", TaskStatus.FAILED),
                task_snapshot("task-3", TaskStatus.PENDING),
            )
        )

        outcome = controller.batch_resume(
            ("task-1", "task-2", "task-3")
        )

        self.assertEqual(outcome.accepted_ids, ("task-1",))
        self.assertEqual(outcome.skipped_ids, ("task-2", "task-3"))
        self.assertEqual(manager.calls, [("resume", "task-1")])

    def test_cancel_accepts_failed_tasks_and_never_enters_delete_flow(self):
        manager, controller = self.make_controller(
            (
                task_snapshot("task-1", TaskStatus.FAILED),
                task_snapshot("task-2", TaskStatus.PAUSED),
                task_snapshot("task-3", TaskStatus.COMPLETED),
            )
        )

        outcome = controller.batch_cancel(
            ("task-1", "task-2", "task-3")
        )

        self.assertEqual(
            outcome.accepted_ids,
            ("task-1", "task-2"),
        )
        self.assertEqual(outcome.skipped_ids, ("task-3",))
        self.assertEqual(
            manager.calls,
            [("cancel", "task-1"), ("cancel", "task-2")],
        )


class BatchTaskPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-batch-page-tests"]
        )

    def setUp(self):
        self.controller = FakeBatchController(
            (
                task_snapshot("task-1", TaskStatus.PENDING),
                task_snapshot("task-2", TaskStatus.PAUSED),
                task_snapshot("task-3", TaskStatus.FAILED),
                task_snapshot("task-4", TaskStatus.PAUSING),
                task_snapshot("task-5", TaskStatus.COMPLETED),
            )
        )
        self.page = DownloadPage(self.controller)
        self.page.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.page.resize(900, 700)
        self.page.show()
        self.page.view_tabs.setCurrentIndex(1)
        self.app.processEvents()

    def tearDown(self):
        self.page.dispose()
        self.page.close()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()

    def test_temporary_mode_selects_only_manageable_rows_and_clears_on_exit(self):
        self.assertFalse(self.page.batch_selection_bar.isVisible())
        self.page.batch_manage_button.click()
        self.app.processEvents()

        self.assertTrue(self.page.batch_selection_bar.isVisible())
        self.assertTrue(
            self.page._task_rows["task-1"].selection_checkbox.isVisible()
        )
        self.assertFalse(
            self.page._task_rows["task-4"].selection_checkbox.isVisible()
        )
        self.assertFalse(
            self.page._task_rows["task-5"].selection_checkbox.isVisible()
        )
        self.assertFalse(
            self.page._task_rows["task-1"].actions_widget.isVisible()
        )

        self.page.batch_select_all_button.click()
        self.assertEqual(
            self.page._batch_selected_ids,
            {"task-1", "task-2", "task-3"},
        )
        self.assertEqual(
            self.page.batch_selected_label.text(),
            "已选 3 个任务",
        )

        self.page.view_tabs.setCurrentIndex(0)
        self.assertFalse(self.page._batch_mode)
        self.assertEqual(self.page._batch_selected_ids, set())

    def test_partial_result_uses_one_summary_and_deleted_rows_are_pruned(self):
        self.page.batch_manage_button.click()
        self.page.batch_select_all_button.click()

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Ok,
        ) as warning:
            self.page.batch_pause_button.click()

        self.assertEqual(len(self.controller.batch_calls), 1)
        self.assertEqual(warning.call_count, 1)
        self.assertIn(
            "已接受 1 个暂停请求",
            self.page.batch_feedback_label.text(),
        )

        self.controller.publish(self.controller.tasks[1:])
        self.app.processEvents()
        self.assertNotIn("task-1", self.page._batch_selected_ids)
        self.assertNotIn("task-1", self.page._task_rows)

    def test_cancel_confirmation_defaults_to_return_and_has_no_delete_action(self):
        self.page.batch_manage_button.click()
        self.page._task_rows["task-1"].selection_checkbox.click()
        captured = {}

        def accept_keep_files(dialog):
            captured["default"] = dialog.defaultButton().text()
            captured["escape"] = dialog.escapeButton().text()
            captured["texts"] = {
                button.text() for button in dialog.buttons()
            }
            target = next(
                button
                for button in dialog.buttons()
                if button.text() == "取消任务并保留文件"
            )
            target.click()
            return dialog.DialogCode.Accepted

        with patch.object(QMessageBox, "exec", accept_keep_files):
            self.page.batch_cancel_button.click()

        self.assertEqual(captured["default"], "返回")
        self.assertEqual(captured["escape"], "返回")
        self.assertFalse(
            any("删除" in text for text in captured["texts"])
        )
        self.assertEqual(
            self.controller.batch_calls,
            [("cancel", ("task-1",))],
        )

    def test_both_themes_define_green_batch_selection_controls(self):
        for theme, color in (
            (Theme.LIGHT, "#2e7d57"),
            (Theme.DARK, "#58b989"),
        ):
            stylesheet = load_stylesheet(theme)
            self.assertIn(
                "QCheckBox#batchTaskSelectionCheck::indicator:checked",
                stylesheet,
            )
            self.assertIn(
                "QFrame#taskBatchSelectionBar",
                stylesheet,
            )
            self.assertIn(color, stylesheet)


if __name__ == "__main__":
    unittest.main()
