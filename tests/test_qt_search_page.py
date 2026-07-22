import os
import unittest
from unittest.mock import patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QPushButton

from jm_downloader.models import (
    FavoriteFolderSnapshot,
    FavoritesSnapshot,
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
    TaskSnapshot,
    TaskStatus,
)
from jm_downloader.qt.main_window import MainWindow
from jm_downloader.qt.theme import ThemeManager
from jm_downloader.qt.widgets.search_result_card import SearchResultCard


class FakeSearchController(QObject):
    search_submitted = Signal(int, object)
    results_ready = Signal(int, object, bool)
    empty_results = Signal(int, object, bool)
    search_failed = Signal(int, str, str, bool)
    validation_failed = Signal(str, str)
    busy_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.service = self
        self.search_calls = []
        self.page_calls = []
        self.retry_count = 0
        self.dispose_count = 0
        self.generation = 0
        self.current_snapshot = None
        self._last_request = None

    def fetch_cover(self, _album_id):
        raise AssertionError("Fake cover loader must keep tests offline")

    def search(self, mode, query, page=1):
        self.search_calls.append((mode, query, page))
        return self._submit(SearchRequest(mode, query, page))

    def change_page(self, page):
        self.page_calls.append(page)
        if self.current_snapshot is None:
            return None
        current = self.current_snapshot.request
        return self._submit(SearchRequest(current.mode, current.query, page))

    def retry(self):
        self.retry_count += 1
        if self._last_request is None:
            return None
        return self._submit(self._last_request)

    def deliver(self, snapshot, *, generation=None, is_page_change=False):
        generation = self.generation if generation is None else generation
        self.current_snapshot = snapshot
        self.busy_changed.emit(False)
        signal = self.results_ready if snapshot.items else self.empty_results
        signal.emit(generation, snapshot, is_page_change)

    def fail(
        self,
        message="搜索失败",
        *,
        generation=None,
        is_page_change=False,
    ):
        generation = self.generation if generation is None else generation
        self.busy_changed.emit(False)
        self.search_failed.emit(
            generation,
            "unavailable",
            message,
            is_page_change,
        )

    def dispose(self):
        self.dispose_count += 1

    def _submit(self, request):
        self.generation += 1
        self._last_request = request
        self.search_submitted.emit(self.generation, request)
        self.busy_changed.emit(True)
        return self.generation


class FakeCoverLoader(QObject):
    cover_ready = Signal(int, str, object)
    cover_failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.dispose_count = 0

    def request(self, generation, album_id, target_size):
        self.requests.append(
            (
                generation,
                album_id,
                target_size.width(),
                target_size.height(),
            )
        )
        return True

    def dispose(self):
        self.dispose_count += 1


class FakeDownloadController(QObject):
    tasks_reset = Signal(object)
    command_failed = Signal(str, str)
    shutdown_finished = Signal(bool)

    def __init__(self):
        super().__init__()
        self.tasks = []
        self.added = []
        self.retried = []
        self.removed = []
        self.opened = []

    def list_tasks(self):
        return list(self.tasks)

    def add_task(self, album_id):
        self.added.append(album_id)
        normalized = str(album_id).strip().upper().removeprefix("JM")
        snapshot = make_task_snapshot(
            task_id=f"task-{len(self.tasks) + 1}",
            album_id=normalized,
        )
        self.tasks.append(snapshot)
        self.tasks_reset.emit(self.list_tasks())
        return snapshot

    def retry_task(self, task_id):
        self.retried.append(task_id)

    def pause_task(self, _task_id):
        pass

    def resume_task(self, _task_id):
        pass

    def cancel_task(self, _task_id, _delete_files=False):
        pass

    def remove_task(self, task_id):
        self.removed.append(task_id)
        self.tasks = [task for task in self.tasks if task.id != task_id]
        self.tasks_reset.emit(self.list_tasks())

    def remove_album(self, album_id):
        self.tasks = [
            task for task in self.tasks if task.album_id != album_id
        ]
        self.tasks_reset.emit(self.list_tasks())

    def open_item(self, album_id, kind):
        self.opened.append((album_id, kind))

    def open_task_item(self, task_id, kind):
        self.opened.append((task_id, kind))

    def has_active_tasks(self):
        return bool(self.tasks)

    def begin_shutdown(self, timeout=5.0):
        del timeout


class FakeFavoritesController(QObject):
    snapshot_changed = Signal(object)
    progress_changed = Signal(object)
    operation_failed = Signal(str, str)
    busy_changed = Signal(bool, str)
    add_succeeded = Signal(str)
    add_failed = Signal(str, str, str)
    add_partially_succeeded = Signal(str, str, str)
    mutation_refresh_failed = Signal(str, str, str)
    filter_result_changed = Signal(int, object)
    mutation_succeeded = Signal(str, str)
    mutation_failed = Signal(str, str, str)
    add_availability_changed = Signal(bool)
    known_favorite_ids_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.current_snapshot = None
        self.is_busy = False
        self.current_command = ""
        self.can_add_favorites = False
        self.known_favorite_ids = frozenset()
        self.current_snapshot = None
        self.add_calls = []
        self.add_targets = []
        self.sync_count = 0

    def add_album(self, album_id, folder_id="0"):
        if (
            not self.can_add_favorites
            or self.is_busy
            or album_id in self.known_favorite_ids
        ):
            return None
        self.add_calls.append(album_id)
        self.add_targets.append(folder_id)
        self.is_busy = True
        self.current_command = "add"
        self.can_add_favorites = False
        self.add_availability_changed.emit(False)
        self.busy_changed.emit(True, "add")
        return len(self.add_calls)

    def set_available(self, available):
        self.can_add_favorites = bool(available) and not self.is_busy
        self.add_availability_changed.emit(self.can_add_favorites)

    def succeed(self, album_id):
        self.is_busy = False
        self.current_command = ""
        self.can_add_favorites = True
        self.busy_changed.emit(False, "")
        self.add_availability_changed.emit(True)
        self.known_favorite_ids = self.known_favorite_ids | {album_id}
        self.known_favorite_ids_changed.emit(self.known_favorite_ids)
        self.add_succeeded.emit(album_id)

    def fail(self, album_id, code="add_uncertain", message="结果无法确认"):
        self.is_busy = False
        self.current_command = ""
        self.can_add_favorites = True
        self.busy_changed.emit(False, "")
        self.add_availability_changed.emit(True)
        self.add_failed.emit(album_id, code, message)

    def sync(self):
        self.sync_count += 1
        return self.sync_count

    @staticmethod
    def filter_items(_folder_id, _keyword):
        return 1

    @staticmethod
    def create_folder(_name):
        return 1

    @staticmethod
    def delete_folder(_folder_id):
        return 1

    @staticmethod
    def move_album(_album_id, _folder_id):
        return 1

    @staticmethod
    def cancel_sync():
        return None

    @staticmethod
    def dispose():
        return None


def make_task_snapshot(task_id="task-1", album_id="1"):
    return TaskSnapshot(
        id=task_id,
        album_id=album_id,
        title=f"测试漫画 {album_id}",
        status=TaskStatus.FETCHING,
        progress=0,
        chapter="",
        page="",
        preview_path=None,
        preview_revision=0,
        pdf_path=None,
        error=None,
        cover_url=None,
    )


def make_search_page(
    request,
    *,
    count=8,
    total=None,
    page_count=1,
    first_id=1,
):
    items = tuple(
        SearchResultSnapshot(
            album_id=str(first_id + index),
            title=f"搜索结果 {first_id + index}",
            authors=("作者",),
            tags=("标签",),
        )
        for index in range(count)
    )
    return SearchPageSnapshot(
        request=request,
        total=count if total is None else total,
        page_count=page_count,
        items=items,
    )


class DownloadSearchPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["download-search-page-tests"]
        )

    def setUp(self):
        self.search_controller = FakeSearchController()
        self.cover_loader = FakeCoverLoader()
        self.download_controller = FakeDownloadController()
        self.favorites_controller = FakeFavoritesController()
        self.theme_manager = ThemeManager()
        self.theme_manager.apply()
        self.cover_patch = patch(
            "jm_downloader.qt.pages.download_page.SearchCoverLoader",
            return_value=self.cover_loader,
        )
        self.cover_patch.start()
        self.target_patch = patch(
            "jm_downloader.qt.pages.download_page.FavoriteTargetDialog.choose",
            return_value="0",
        )
        self.target_choose = self.target_patch.start()
        self.window = MainWindow(
            self.theme_manager,
            self.download_controller,
            search_controller=self.search_controller,
            favorites_controller=self.favorites_controller,
            persist_window_state=False,
        )
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen,
            True,
        )
        self.window.show()
        self.page = self.window.page("downloads")
        self._pump()

    def tearDown(self):
        self.download_controller.tasks = []
        self.window._shutdown_complete = True
        self.window.close()
        self._pump()
        self.cover_patch.stop()
        self.target_patch.stop()
        self.cover_loader.deleteLater()
        self.search_controller.deleteLater()
        self.download_controller.deleteLater()
        self.favorites_controller.deleteLater()
        self._pump()

    def _pump(self, rounds=4):
        for _ in range(rounds):
            self.app.processEvents()

    def _search_and_deliver(self, snapshot):
        generation = self.search_controller.search(
            snapshot.request.mode,
            snapshot.request.query,
            snapshot.request.page,
        )
        self.search_controller.deliver(snapshot, generation=generation)
        self._pump()
        return generation

    def test_favorite_target_can_be_custom_or_cancelled(self):
        request = SearchRequest(SearchMode.EXACT_ID, "1449491", 1)
        self._search_and_deliver(make_search_page(request, count=1, first_id=1449491))
        self.favorites_controller.current_snapshot = FavoritesSnapshot(
            "2026-07-22T12:00:00Z",
            (
                FavoriteFolderSnapshot("0", "全部收藏", ()),
                FavoriteFolderSnapshot("9", "Reading", ()),
            ),
        )
        self.favorites_controller.set_available(True)
        self.target_choose.return_value = "9"

        self.page.comic_cards[0].favorite_button.click()

        self.assertEqual(self.favorites_controller.add_calls, ["1449491"])
        self.assertEqual(self.favorites_controller.add_targets, ["9"])

        self.favorites_controller.fail("1449491")
        self.favorites_controller.add_calls.clear()
        self.target_choose.return_value = None
        self.page.comic_cards[0].favorite_button.click()
        self.assertEqual(self.favorites_controller.add_calls, [])

    def test_partial_move_failure_is_non_modal_and_honest(self):
        request = SearchRequest(SearchMode.EXACT_ID, "1449491", 1)
        self._search_and_deliver(make_search_page(request, count=1, first_id=1449491))
        self.favorites_controller.set_available(True)
        self.page.comic_cards[0].favorite_button.click()

        self.favorites_controller.add_partially_succeeded.emit(
            "1449491",
            "move_failed_after_add",
            "已收藏，但移动失败，当前位于默认位置",
        )
        self._pump()

        self.assertTrue(self.page.favorite_feedback_banner.property("error"))
        self.assertEqual(
            self.page.favorite_feedback_label.text(),
            "已收藏，但移动失败，当前位于默认位置",
        )

    def test_modes_placeholders_and_both_search_commands_only_route_search(self):
        expectations = (
            (SearchMode.GENERAL, "搜索漫画名、标签或作者"),
            (SearchMode.AUTHOR, "搜索作者名称"),
            (SearchMode.TAG, "搜索标签"),
        )
        for index, (mode, placeholder) in enumerate(expectations):
            with self.subTest(mode=mode):
                self.page._mode_buttons[mode].click()
                self.assertIs(self.page.search_mode, mode)
                self.assertEqual(
                    self.page.general_search_input.placeholderText(),
                    placeholder,
                )

                query = f"query-{index}"
                self.page.general_search_input.setText(query)
                self.page.general_search_input.returnPressed.emit()
                self.assertEqual(
                    self.search_controller.search_calls[-1],
                    (mode, query, 1),
                )
                self.page.general_search_button.click()
                self.assertEqual(
                    self.search_controller.search_calls[-1],
                    (mode, query, 1),
                )

        self.page.jm_id_search_input.setText("JM1449491")
        self.page.jm_id_search_input.returnPressed.emit()
        self.assertEqual(
            self.search_controller.search_calls[-1],
            (SearchMode.EXACT_ID, "JM1449491", 1),
        )
        self.page.jm_id_search_button.click()
        self.assertEqual(
            self.search_controller.search_calls[-1],
            (SearchMode.EXACT_ID, "JM1449491", 1),
        )
        self.assertEqual(self.download_controller.added, [])

    def test_idle_loading_results_empty_error_and_retry_states(self):
        self.assertEqual(self.page.search_state, "idle")

        self.search_controller.validation_failed.emit(
            "validation",
            "搜索内容不能为空",
        )
        self.assertEqual(self.page.search_state, "idle")
        self.assertEqual(
            self.page.results_summary.text(),
            "搜索内容不能为空",
        )
        self.assertTrue(self.page.results_summary.property("error"))

        request = SearchRequest(SearchMode.GENERAL, "状态")
        generation = self.search_controller.search(
            request.mode,
            request.query,
        )
        self.assertEqual(self.page.search_state, "loading")
        self.assertEqual(self.page.results_summary.text(), "正在搜索...")
        self.assertFalse(self.page.results_summary.property("error"))

        results = make_search_page(request, count=3, total=7, page_count=3)
        self.search_controller.deliver(results, generation=generation)
        self._pump()
        self.assertEqual(self.page.search_state, "results")
        self.assertEqual(len(self.page.comic_cards), 3)
        self.assertEqual(self.page.results_summary.text(), "共 7 条 · 第 1 / 3 页")

        limited = SearchPageSnapshot(
            request,
            1000,
            13,
            results.items,
            truncated=True,
        )
        self.page._update_result_summary(limited)
        self.assertEqual(
            self.page.results_summary.text(),
            "最多展示 1000 条 · 第 1 / 13 页",
        )

        empty_request = SearchRequest(SearchMode.TAG, "不存在")
        empty_generation = self.search_controller.search(
            empty_request.mode,
            empty_request.query,
        )
        empty = make_search_page(
            empty_request,
            count=0,
            total=0,
            page_count=0,
        )
        self.search_controller.deliver(empty, generation=empty_generation)
        self._pump()
        self.assertEqual(self.page.search_state, "empty")
        self.assertEqual(self.page.results_summary.text(), "共 0 条")
        self.assertEqual(self.page.comic_cards, ())

        error_request = SearchRequest(SearchMode.AUTHOR, "失败")
        error_generation = self.search_controller.search(
            error_request.mode,
            error_request.query,
        )
        self.search_controller.fail(
            "服务暂时不可用",
            generation=error_generation,
        )
        self._pump()
        self.assertEqual(self.page.search_state, "error")
        self.assertEqual(self.page.search_error_label.text(), "服务暂时不可用")

        retry_button = self.page._search_states["error"].findChild(
            QPushButton,
            "retrySearchButton",
        )
        retry_button.click()
        self.assertEqual(self.search_controller.retry_count, 1)
        self.assertEqual(self.page.search_state, "loading")

    def test_pagination_boundaries_failed_page_retention_and_retry(self):
        request = SearchRequest(SearchMode.GENERAL, "分页")
        first_page = make_search_page(
            request,
            count=4,
            total=12,
            page_count=3,
        )
        self._search_and_deliver(first_page)

        self.assertFalse(self.page.previous_page_button.isEnabled())
        self.assertTrue(self.page.next_page_button.isEnabled())
        self.assertFalse(self.page.pagination.isHidden())
        self.assertEqual(self.page.page_label.text(), "第 1 / 3 页")
        original_cards = self.page.comic_cards

        self.page.next_page_button.click()
        failed_generation = self.search_controller.generation
        self.assertEqual(self.search_controller.page_calls, [2])
        self.assertTrue(
            all(not card.action_button.isEnabled() for card in original_cards)
        )
        self.assertEqual(self.page.search_state, "results")
        self.assertEqual(self.page.results_summary.text(), "正在载入第 2 页...")

        self.search_controller.fail(
            "第二页加载失败",
            generation=failed_generation,
            is_page_change=True,
        )
        self._pump()
        self.assertEqual(self.page.search_state, "results")
        self.assertEqual(self.page.comic_cards, original_cards)
        self.assertFalse(self.page.page_error_banner.isHidden())
        self.assertEqual(self.page.page_error_label.text(), "第二页加载失败")
        self.assertEqual(self.page.page_label.text(), "第 1 / 3 页")
        self.assertTrue(
            all(card.action_button.isEnabled() for card in original_cards)
        )

        self.page.page_retry_button.click()
        retried_generation = self.search_controller.generation
        self.assertEqual(self.search_controller.retry_count, 1)
        self.assertEqual(self.page.search_state, "results")
        self.assertEqual(self.page.results_summary.text(), "正在载入第 2 页...")

        second_request = SearchRequest(SearchMode.GENERAL, "分页", 2)
        second_page = make_search_page(
            second_request,
            count=4,
            total=12,
            page_count=3,
            first_id=5,
        )
        self.search_controller.deliver(
            second_page,
            generation=retried_generation,
            is_page_change=True,
        )
        self._pump()
        self.assertTrue(self.page.previous_page_button.isEnabled())
        self.assertTrue(self.page.next_page_button.isEnabled())
        self.assertEqual(self.page.page_label.text(), "第 2 / 3 页")

        self.page.next_page_button.click()
        third_generation = self.search_controller.generation
        third_request = SearchRequest(SearchMode.GENERAL, "分页", 3)
        third_page = make_search_page(
            third_request,
            count=4,
            total=12,
            page_count=3,
            first_id=9,
        )
        self.search_controller.deliver(
            third_page,
            generation=third_generation,
            is_page_change=True,
        )
        self._pump()
        self.assertTrue(self.page.previous_page_button.isEnabled())
        self.assertFalse(self.page.next_page_button.isEnabled())
        self.assertEqual(self.page.page_label.text(), "第 3 / 3 页")

        page_calls = list(self.search_controller.page_calls)
        self.page.next_page_button.click()
        self.assertEqual(self.search_controller.page_calls, page_calls)
        self.page.previous_page_button.click()
        self.assertEqual(self.search_controller.page_calls[-1], 2)

    def test_responsive_grid_and_visible_cover_requests_are_bounded_and_unique(self):
        self.window.resize(760, 520)
        self._pump()
        request = SearchRequest(SearchMode.GENERAL, "大量结果")
        snapshot = make_search_page(
            request,
            count=80,
            total=80,
            page_count=1,
        )
        self._search_and_deliver(snapshot)

        self.assertEqual(self.page.column_count, 2)
        self.assertTrue(self.page.pagination.isHidden())
        requested_ids = [request[1] for request in self.cover_loader.requests]
        self.assertGreater(len(requested_ids), 0)
        self.assertLess(len(requested_ids), 20)
        self.assertEqual(len(requested_ids), len(set(requested_ids)))

        scroll_bar = self.page.results_scroll.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self._pump()
        self.page._request_visible_covers()
        self.page._request_visible_covers()
        self.window.resize(760, 520)
        self._pump()
        keys = [request[:2] for request in self.cover_loader.requests]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertLess(len(keys), 80)

        self.window.resize(1100, 720)
        self._pump()
        self.assertEqual(self.page.column_count, 4)
        self.page._request_visible_covers()
        keys = [request[:2] for request in self.cover_loader.requests]
        self.assertEqual(len(keys), len(set(keys)))

    def test_new_results_reset_scroll_but_tab_round_trip_and_enqueue_preserve_it(self):
        self.window.resize(760, 520)
        self._pump()
        request = SearchRequest(SearchMode.GENERAL, "滚动")
        snapshot = make_search_page(request, count=40, total=40)
        self._search_and_deliver(snapshot)

        scroll_bar = self.page.results_scroll.verticalScrollBar()
        self.assertGreater(scroll_bar.maximum(), 0)
        scroll_bar.setValue(scroll_bar.maximum())
        self._pump()
        retained_value = scroll_bar.value()

        self.page.view_tabs.setCurrentIndex(1)
        self.page.view_tabs.setCurrentIndex(0)
        self._pump()
        self.assertEqual(scroll_bar.value(), retained_value)

        self.page.comic_cards[-1].action_button.click()
        self._pump()
        self.assertEqual(self.page.view_tabs.currentIndex(), 0)
        self.assertEqual(scroll_bar.value(), retained_value)
        self.assertEqual(self.download_controller.added, ["40"])

        replacement_request = SearchRequest(SearchMode.TAG, "新结果")
        replacement = make_search_page(
            replacement_request,
            count=20,
            total=20,
            first_id=101,
        )
        self._search_and_deliver(replacement)
        self.assertEqual(scroll_bar.value(), 0)

    def test_old_generation_cover_is_dropped(self):
        first_request = SearchRequest(SearchMode.GENERAL, "旧请求")
        first = make_search_page(first_request, count=1)
        old_generation = self._search_and_deliver(first)

        second_request = SearchRequest(SearchMode.GENERAL, "新请求")
        second = make_search_page(second_request, count=1)
        current_generation = self._search_and_deliver(second)
        card = self.page.comic_cards[0]

        old_image = QImage(24, 24, QImage.Format.Format_RGB32)
        old_image.fill(0xFFCC3344)
        self.cover_loader.cover_ready.emit(
            old_generation,
            card.snapshot.album_id,
            old_image,
        )
        self._pump()
        self.assertTrue(card.cover_label.pixmap().isNull())

        current_image = QImage(24, 24, QImage.Format.Format_RGB32)
        current_image.fill(0xFF247A52)
        self.cover_loader.cover_ready.emit(
            current_generation,
            card.snapshot.album_id,
            current_image,
        )
        self._pump()
        self.assertFalse(card.cover_label.pixmap().isNull())

        self.cover_loader.cover_failed.emit(
            old_generation,
            card.snapshot.album_id,
        )
        self._pump()
        self.assertFalse(card.cover_label.pixmap().isNull())

    def test_card_enqueues_once_views_existing_task_and_recovers_after_removal(self):
        request = SearchRequest(SearchMode.EXACT_ID, "1449491")
        snapshot = SearchPageSnapshot(
            request=request,
            total=1,
            page_count=1,
            items=(
                SearchResultSnapshot(
                    album_id="1449491",
                    title="测试漫画",
                    authors=("作者",),
                    tags=("标签",),
                ),
            ),
        )
        self._search_and_deliver(snapshot)
        card = self.page.comic_cards[0]
        self.assertTrue(self.page.pagination.isHidden())
        self.assertEqual(self.page.results_summary.text(), "共 1 条")

        card.action_button.click()
        self._pump()
        self.assertEqual(self.download_controller.added, ["1449491"])
        self.assertTrue(card.task_present)
        self.assertEqual(card.action_button.text(), "查看任务")

        card.action_button.click()
        self._pump()
        self.assertEqual(self.download_controller.added, ["1449491"])
        self.assertEqual(self.page.view_tabs.currentIndex(), 1)

        self.download_controller.remove_album("1449491")
        self._pump()
        self.assertFalse(card.task_present)
        self.assertEqual(card.action_button.text(), "下载")

        self.page.view_tabs.setCurrentIndex(0)
        card.action_button.click()
        self._pump()
        self.assertEqual(
            self.download_controller.added,
            ["1449491", "1449491"],
        )

    def test_favorite_button_tracks_login_busy_success_and_duplicate_cards(self):
        request = SearchRequest(SearchMode.GENERAL, "重复结果")
        item = SearchResultSnapshot(
            album_id="1449491",
            title="测试漫画",
            authors=("作者",),
            tags=("标签",),
        )
        snapshot = SearchPageSnapshot(request, 2, 1, (item, item))
        self._search_and_deliver(snapshot)
        buttons = [card.favorite_button for card in self.page.comic_cards]

        self.assertTrue(all(not button.isHidden() for button in buttons))
        self.assertTrue(all(not button.isEnabled() for button in buttons))
        self.assertTrue(
            all(card.action_button.isEnabled() for card in self.page.comic_cards)
        )

        self.favorites_controller.set_available(True)
        self._pump()
        self.assertTrue(all(button.isEnabled() for button in buttons))

        buttons[0].click()
        self._pump()
        self.assertEqual(self.favorites_controller.add_calls, ["1449491"])
        self.assertTrue(all(not button.isEnabled() for button in buttons))
        self.assertTrue(
            all(card.favorite_busy for card in self.page.comic_cards)
        )
        self.assertTrue(
            all(card.action_button.isEnabled() for card in self.page.comic_cards)
        )

        self.favorites_controller.succeed("1449491")
        self._pump()
        self.assertTrue(all(card.favorited for card in self.page.comic_cards))
        self.assertTrue(all(button.isChecked() for button in buttons))
        self.assertTrue(all(not button.isEnabled() for button in buttons))
        self.assertFalse(self.page.favorite_feedback_banner.isHidden())
        self.assertEqual(
            self.page.favorite_feedback_label.text(),
            "已添加到未分类并刷新收藏",
        )
        self.assertEqual(self.favorites_controller.sync_count, 0)

        buttons[1].click()
        self.assertEqual(self.favorites_controller.add_calls, ["1449491"])

        self.search_controller.search(SearchMode.TAG, "下一次搜索")
        self._pump()
        self.assertTrue(self.page.favorite_feedback_banner.isHidden())

    def test_favorite_failure_is_non_modal_and_restores_the_button(self):
        request = SearchRequest(SearchMode.EXACT_ID, "350234")
        snapshot = make_search_page(request, count=1, first_id=350234)
        self._search_and_deliver(snapshot)
        card = self.page.comic_cards[0]
        self.favorites_controller.set_available(True)
        self._pump()

        with patch(
            "jm_downloader.qt.pages.download_page.QMessageBox.warning"
        ) as warning:
            card.favorite_button.click()
            self.favorites_controller.fail(
                "350234",
                message="收藏结果无法确认，请手动同步",
            )
            self._pump()

        warning.assert_not_called()
        self.assertFalse(card.favorited)
        self.assertFalse(card.favorite_busy)
        self.assertTrue(card.favorite_button.isEnabled())
        self.assertFalse(self.page.favorite_feedback_banner.isHidden())
        self.assertTrue(self.page.favorite_feedback_banner.property("error"))
        self.assertEqual(
            self.page.favorite_feedback_label.text(),
            "收藏结果无法确认，请手动同步",
        )
        self.assertEqual(self.favorites_controller.sync_count, 0)

    def test_known_favorite_state_survives_new_results(self):
        self.favorites_controller.known_favorite_ids = frozenset({"7"})
        self.favorites_controller.set_available(True)
        request = SearchRequest(SearchMode.AUTHOR, "作者")
        self._search_and_deliver(
            make_search_page(request, count=2, first_id=7)
        )

        first, second = self.page.comic_cards
        self.assertTrue(first.favorited)
        self.assertFalse(first.favorite_button.isEnabled())
        self.assertFalse(second.favorited)
        self.assertTrue(second.favorite_button.isEnabled())

    def test_dispose_stops_cover_loader_once(self):
        self.page.dispose()
        self.page.dispose()
        self.assertEqual(self.cover_loader.dispose_count, 1)


if __name__ == "__main__":
    unittest.main()
