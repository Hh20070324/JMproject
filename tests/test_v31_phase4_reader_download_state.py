import os
import threading
import time
import unittest


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderChapterDownloadSnapshot,
    ReaderChapterDownloadState,
    ReaderChapterSnapshot,
    TaskSnapshot,
    TaskStatus,
)
from jm_downloader.qt.controllers.reader_controller import ReaderController
from jm_downloader.qt.controllers.reader_download_controller import (
    ReaderDownloadController,
)
from jm_downloader.qt.pages.reader_page import ReaderPage


def task_snapshot(
    task_id: str,
    status: TaskStatus,
    *,
    album_id: str = "100",
    selected_chapter_ids: tuple[str, ...] | None = ("301",),
) -> TaskSnapshot:
    return TaskSnapshot(
        id=task_id,
        album_id=album_id,
        title="测试漫画",
        status=status,
        progress=0,
        chapter="",
        page="",
        preview_path=None,
        preview_revision=0,
        pdf_directory=None,
        error=None,
        cover_url=None,
        selected_chapter_ids=selected_chapter_ids,
    )


class FakeDownloadController(QObject):
    tasks_reset = Signal(object)

    def __init__(self, tasks=()):
        super().__init__()
        self.tasks = list(tasks)

    def list_tasks(self):
        return list(self.tasks)

    def publish(self, tasks):
        self.tasks = list(tasks)
        self.tasks_reset.emit(self.list_tasks())


class FakeDownloadStateController(QObject):
    state_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.requested = []
        self.retried = 0
        self.cleared = 0

    def request(self, album_id, photo_id):
        self.requested.append((album_id, photo_id))

    def retry(self):
        self.retried += 1

    def clear(self):
        self.cleared += 1


class IdleReaderService:
    async def fetch_catalog(self, _album_id):
        raise AssertionError

    async def load_chapter(self, _catalog, _photo_id):
        raise AssertionError

    async def fetch_page(self, *_args, **_kwargs):
        raise AssertionError

    async def close(self):
        return True


class ReaderDownloadControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-download-state-tests"]
        )

    def setUp(self):
        self.downloads = FakeDownloadController()
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.dispose()
            controller.deleteLater()
        self.downloads.deleteLater()
        self.app.processEvents()

    def make_controller(self, detector):
        controller = ReaderDownloadController(
            detector,
            self.downloads,
        )
        self.controllers.append(controller)
        return controller

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timed out waiting for reader download state")

    def test_disk_detection_runs_off_main_thread_and_publishes_available(self):
        thread_ids = []
        states = []
        controller = self.make_controller(
            lambda _album_id: thread_ids.append(threading.get_ident()) or ()
        )
        controller.state_changed.connect(states.append)

        controller.request("100", "301")
        self.wait_until(
            lambda: states
            and states[-1].state is ReaderChapterDownloadState.AVAILABLE
        )

        self.assertEqual(len(thread_ids), 1)
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.assertEqual(
            [snapshot.state for snapshot in states],
            [
                ReaderChapterDownloadState.CHECKING,
                ReaderChapterDownloadState.AVAILABLE,
            ],
        )

    def test_downloaded_and_every_non_completed_task_reserve_the_chapter(self):
        states = []
        controller = self.make_controller(lambda _album_id: {"301"})
        controller.state_changed.connect(states.append)
        controller.request("100", "301")
        self.wait_until(
            lambda: states
            and states[-1].state is ReaderChapterDownloadState.DOWNLOADED
        )

        for status in (
            TaskStatus.PENDING,
            TaskStatus.FETCHING,
            TaskStatus.DOWNLOADING,
            TaskStatus.PAUSING,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
        ):
            self.downloads.publish(
                [task_snapshot(f"task-{status.value}", status)]
            )
            self.app.processEvents()
            self.assertIs(
                states[-1].state,
                ReaderChapterDownloadState.TASK_RESERVED,
            )
            self.assertIs(states[-1].task_status, status)

        self.downloads.publish(
            [
                task_snapshot(
                    "whole-album",
                    TaskStatus.PAUSED,
                    selected_chapter_ids=None,
                )
            ]
        )
        self.app.processEvents()
        self.assertIs(
            states[-1].state,
            ReaderChapterDownloadState.TASK_RESERVED,
        )

    def test_latest_mailbox_discards_late_and_superseded_results(self):
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []
        states = []

        def detector(album_id):
            calls.append(album_id)
            if album_id == "100":
                first_started.set()
                release_first.wait(1.0)
            return ()

        controller = self.make_controller(detector)
        controller.state_changed.connect(states.append)
        controller.request("100", "301")
        self.assertTrue(first_started.wait(1.0))
        controller.request("200", "401")
        controller.request("300", "501")
        release_first.set()

        self.wait_until(
            lambda: states
            and states[-1].album_id == "300"
            and states[-1].state is ReaderChapterDownloadState.AVAILABLE
        )

        self.assertEqual(calls, ["100", "300"])
        self.assertFalse(
            any(
                snapshot.album_id in {"100", "200"}
                and snapshot.state is ReaderChapterDownloadState.AVAILABLE
                for snapshot in states
            )
        )

    def test_failure_is_unknown_and_retry_only_rechecks(self):
        attempts = 0
        states = []

        def detector(_album_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("private path must not escape")
            return ()

        controller = self.make_controller(detector)
        controller.state_changed.connect(states.append)
        controller.request("100", "301")
        self.wait_until(
            lambda: states
            and states[-1].state is ReaderChapterDownloadState.UNKNOWN
        )
        self.assertEqual(states[-1].message, "无法确认下载状态")

        controller.retry()
        self.wait_until(
            lambda: attempts == 2
            and states[-1].state is ReaderChapterDownloadState.AVAILABLE
        )

    def test_completed_task_transition_rescans_after_manifest_publish(self):
        scans = 0
        states = []

        def detector(_album_id):
            nonlocal scans
            scans += 1
            return {"301"} if scans >= 2 else ()

        controller = self.make_controller(detector)
        controller.state_changed.connect(states.append)
        controller.request("100", "301")
        self.wait_until(
            lambda: states
            and states[-1].state is ReaderChapterDownloadState.AVAILABLE
        )

        self.downloads.publish(
            [task_snapshot("task-1", TaskStatus.DOWNLOADING)]
        )
        self.app.processEvents()
        self.assertIs(
            states[-1].state,
            ReaderChapterDownloadState.TASK_RESERVED,
        )

        self.downloads.publish(
            [task_snapshot("task-1", TaskStatus.COMPLETED)]
        )
        self.wait_until(
            lambda: scans == 2
            and states[-1].state is ReaderChapterDownloadState.DOWNLOADED
        )
        self.assertIn(
            ReaderChapterDownloadState.CHECKING,
            [snapshot.state for snapshot in states],
        )


class ReaderDownloadButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v31-reader-download-button-tests"]
        )

    def setUp(self):
        self.reader_controller = ReaderController(
            IdleReaderService(),
            result_interval_ms=5,
        )
        self.download_state = FakeDownloadStateController()
        self.page = ReaderPage(
            self.reader_controller,
            download_state_controller=self.download_state,
        )
        self.page._catalog = ChapterCatalogSnapshot(
            "100",
            "测试漫画",
            (ChapterSnapshot("301", 1, "第一章"),),
        )
        self.page._chapter = ReaderChapterSnapshot(
            "301",
            1,
            "第一章",
            10,
        )
        self.page._download_photo_id = "301"
        self.requested_downloads = []
        self.page.download_chapter_requested.connect(
            self.requested_downloads.append
        )

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.reader_controller.shutdown(1.0)
        self.reader_controller.deleteLater()
        self.download_state.deleteLater()
        self.app.processEvents()

    def publish(self, state, message):
        self.download_state.state_changed.emit(
            ReaderChapterDownloadSnapshot(
                "100",
                "301",
                state,
                message,
            )
        )
        self.app.processEvents()

    def test_only_available_state_can_submit_download(self):
        self.publish(ReaderChapterDownloadState.AVAILABLE, "下载当前章节")
        self.assertTrue(self.page.download_button.isEnabled())
        self.page.download_button.click()
        self.assertEqual(self.requested_downloads, ["301"])

        for state, message in (
            (
                ReaderChapterDownloadState.CHECKING,
                "正在检查下载状态…",
            ),
            (
                ReaderChapterDownloadState.TASK_RESERVED,
                "当前章节已有任务",
            ),
            (
                ReaderChapterDownloadState.DOWNLOADED,
                "当前章节已下载",
            ),
        ):
            self.publish(state, message)
            self.assertFalse(self.page.download_button.isEnabled())
            self.page._download_current()
        self.assertEqual(self.requested_downloads, ["301"])

    def test_unknown_state_exposes_recheck_instead_of_download(self):
        self.publish(
            ReaderChapterDownloadState.UNKNOWN,
            "无法确认下载状态",
        )

        self.assertTrue(self.page.download_button.isEnabled())
        self.assertEqual(
            self.page.download_button.text(),
            "重新检查下载状态",
        )
        self.page.download_button.click()

        self.assertEqual(self.download_state.retried, 1)
        self.assertEqual(self.requested_downloads, [])


if __name__ == "__main__":
    unittest.main()
