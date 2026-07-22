import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.models import (
    AccountSnapshot,
    AccountStatus,
    FavoriteFolderSnapshot,
    FavoriteItemSnapshot,
    FavoritesSnapshot,
    FavoritesFilterSnapshot,
)
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.qt.pages.favorites_page import FavoritesPage
from jm_downloader.qt.theme import Theme, load_stylesheet
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


class PageProtector:
    def protect(self, plaintext):
        return b"page\0" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"page\0"):
            raise ValueError("invalid")
        return ciphertext[len(b"page\0") :][::-1]


class FakeFavoritesController(QObject):
    snapshot_changed = Signal(object)
    progress_changed = Signal(object)
    operation_failed = Signal(str, str)
    busy_changed = Signal(bool, str)
    filter_result_changed = Signal(int, object)
    mutation_succeeded = Signal(str, str)
    mutation_failed = Signal(str, str, str)
    mutation_refresh_failed = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.current_snapshot = None
        self.is_busy = False
        self.current_command = ""
        self.sync = Mock(return_value=1)
        self.cancel_sync = Mock()
        self.filter_items = Mock(return_value=1)
        self.create_folder = Mock(return_value=1)
        self.delete_folder = Mock(return_value=1)
        self.move_album = Mock(return_value=1)


class FakeDownloadController(QObject):
    tasks_reset = Signal(object)

    def __init__(self):
        super().__init__()
        self.add_task = Mock(return_value=object())

    @staticmethod
    def list_tasks():
        return []


class FakeCoverLoader(QObject):
    cover_ready = Signal(int, str, object)
    cover_failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, generation, album_id, size):
        self.requests.append((generation, album_id, size))
        return True

    def dispose(self):
        pass


class FavoritesPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["favorites-page-tests"]
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        protector = PageProtector()
        service = AccountService(
            self.paths,
            account_store=AccountStore(
                ProtectedStore.account(self.paths, protector)
            ),
            favorites_store=ProtectedStore.favorites(self.paths, protector),
            client_factory=lambda _cookies: FakeJmAccountClient(),
        )
        self.controller = AccountController(
            service,
            result_interval_ms=5,
            auto_restore=False,
        )
        self.favorites_controller = FakeFavoritesController()
        self.download_controller = FakeDownloadController()
        self.cover_loader = FakeCoverLoader()
        self.page = FavoritesPage(
            self.controller,
            favorites_controller=self.favorites_controller,
            download_controller=self.download_controller,
            cover_loader=self.cover_loader,
        )
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.resize(552, 520)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.controller.dispose()
        for worker in self.controller._workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.controller.deleteLater()
        self.favorites_controller.deleteLater()
        self.download_controller.deleteLater()
        self.cover_loader.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_password_is_masked_and_cleared_immediately_on_submit(self):
        self.assertEqual(
            self.page.password_input.echoMode(),
            QLineEdit.EchoMode.Password,
        )
        self.controller.login = Mock(return_value=1)
        self.page.username_input.setText("account-name")
        self.page.password_input.setText("private-password")

        self.page.login_button.click()

        self.assertEqual(self.page.password_input.text(), "")
        self.controller.login.assert_called_once_with(
            "account-name",
            "private-password",
        )

    def test_account_states_render_without_exposing_session_data(self):
        self.page._on_snapshot(AccountSnapshot(AccountStatus.RESTORING))
        self.assertIs(
            self.page.state_stack.currentWidget(),
            self.page.loading_state,
        )

        self.page._on_snapshot(
            AccountSnapshot(
                AccountStatus.SAVED_SESSION,
                "saved-user",
                "2026-07-16T08:30:00Z",
            )
        )
        self.assertIs(
            self.page.state_stack.currentWidget(),
            self.page.account_state,
        )
        self.assertEqual(self.page.account_name.text(), "saved-user")
        self.assertIn("尚未联网验证", self.page.account_status.text())
        visible_text = " ".join(
            label.text() for label in self.page.findChildren(type(self.page.account_name))
        )
        self.assertNotIn("cookie", visible_text.lower())

        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.EXPIRED, "saved-user")
        )
        self.assertIs(
            self.page.state_stack.currentWidget(),
            self.page.account_state,
        )
        self.assertTrue(self.page.expired_panel.isVisible())
        self.assertFalse(self.page.sync_button.isEnabled())

        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.LOCAL_DATA_UNREADABLE)
        )
        self.assertTrue(self.page.clear_button.isVisible())
        self.assertIn("无法读取", self.page.login_title.text())

    def test_login_error_is_inline_and_cleared_by_new_snapshot(self):
        self.page._on_operation_failed("rejected", "账号或密码错误")
        self.assertTrue(self.page.login_error.isVisible())
        self.assertEqual(self.page.login_error.text(), "账号或密码错误")

        self.page._on_snapshot(AccountSnapshot(AccountStatus.SIGNED_OUT))

        self.assertFalse(self.page.login_error.isVisible())
        self.assertEqual(self.page.login_error.text(), "")

    def test_logout_and_clear_require_native_confirmation(self):
        self.controller.logout = Mock(return_value=1)
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "user")
        )
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.page.logout_button.click()
        self.controller.logout.assert_not_called()

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.page.logout_button.click()
        self.controller.logout.assert_called_once_with()

        self.controller.logout.reset_mock()
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.LOCAL_DATA_UNREADABLE)
        )
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.page.clear_button.click()
        self.controller.logout.assert_called_once_with()

    def test_compact_layout_keeps_inputs_and_actions_separate(self):
        self.page._on_snapshot(AccountSnapshot(AccountStatus.SIGNED_OUT))
        self.app.processEvents()

        self.assertLessEqual(
            self.page.username_input.geometry().right(),
            self.page.login_state.width(),
        )
        self.assertFalse(
            self.page.username_input.geometry().intersects(
                self.page.password_input.geometry()
            )
        )
        self.assertFalse(
            self.page.clear_button.geometry().intersects(
                self.page.login_button.geometry()
            )
        )
        self.assertEqual(self.page.login_button.height(), 38)

    def test_expired_relogin_password_is_masked_and_cleared(self):
        self.controller.login = Mock(return_value=1)
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.EXPIRED, "saved-user")
        )
        self.page.expired_password_input.setText("private-password")

        self.page.relogin_button.click()

        self.assertEqual(
            self.page.expired_password_input.echoMode(),
            QLineEdit.EchoMode.Password,
        )
        self.assertEqual(self.page.expired_password_input.text(), "")
        self.controller.login.assert_called_once_with(
            "saved-user",
            "private-password",
        )

    def test_snapshot_uses_bounded_local_pages_and_existing_cards(self):
        items = tuple(
            FavoriteItemSnapshot(str(index), f"Title {index}")
            for index in range(1, 26)
        )
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot("0", "Default", items),
                FavoriteFolderSnapshot(
                    "9",
                    "Other",
                    (FavoriteItemSnapshot("99", "Other item"),),
                ),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )

        self.page._on_favorites_snapshot(snapshot)
        self.assertTrue(
            self.page.favorite_cards[0].favorite_button.isHidden()
        )
        self.app.processEvents()

        self.assertIs(
            self.page.favorites_stack.currentWidget(),
            self.page.favorite_results_state,
        )
        self.assertEqual(len(self.page.folder_menu.actions()), 2)
        self.assertEqual(len(self.page.favorite_cards), 20)
        self.assertEqual(self.page.favorite_cards[0].snapshot.album_id, "1")
        self.page.next_page_button.click()
        self.app.processEvents()
        self.assertEqual(len(self.page.favorite_cards), 5)
        self.assertEqual(self.page.favorite_cards[0].snapshot.album_id, "21")
        self.assertEqual(self.page.page_label.text(), "第 2 / 2 页")
        self.page.folder_menu.actions()[1].trigger()
        self.app.processEvents()
        self.assertEqual(len(self.page.favorite_cards), 1)
        self.assertEqual(self.page.favorite_cards[0].snapshot.album_id, "99")
        self.assertEqual(self.page.favorites_summary.text(), "共 1 条")

    def test_popup_click_switches_folder_with_one_selection(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot("0", "全部收藏", ()),
                FavoriteFolderSnapshot("9", "Reading", ()),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)

        self.page.keyword_input.setText("alpha")
        self.favorites_controller.filter_items.reset_mock()
        target = self.page.folder_menu.actions()[1]
        QTimer.singleShot(
            50,
            lambda: QTest.mouseClick(
                self.page.folder_menu,
                Qt.MouseButton.LeftButton,
                pos=self.page.folder_menu.actionGeometry(target).center(),
            ),
        )
        QTest.mouseClick(
            self.page.folder_button,
            Qt.MouseButton.LeftButton,
        )
        self.app.processEvents()

        self.assertEqual(self.page.folder_button.text(), "Reading (0)  ▾")
        self.assertEqual(self.page._selected_folder_id, "9")
        self.assertEqual(
            sum(
                action.isChecked()
                for action in self.page.folder_menu.actions()
            ),
            1,
        )
        self.assertTrue(
            next(
                action
                for action in self.page.folder_menu.actions()
                if action.data() == "9"
            ).isChecked()
        )
        self.favorites_controller.filter_items.assert_called_once_with(
            "9", "alpha"
        )

        refreshed = FavoritesSnapshot(
            "2026-07-16T12:50:00Z",
            (
                FavoriteFolderSnapshot("0", "全部收藏", ()),
                FavoriteFolderSnapshot(
                    "9",
                    "Reading",
                    (FavoriteItemSnapshot("2", "Beta"),),
                ),
            ),
        )
        self.page._on_favorites_snapshot(refreshed)

        self.assertEqual(self.page._selected_folder_id, "9")
        self.assertEqual(self.page.folder_button.text(), "Reading (1)  ▾")
        self.assertEqual(
            [
                action.data()
                for action in self.page.folder_menu.actions()
                if action.isChecked()
            ],
            ["9"],
        )

    def test_folder_switch_is_disabled_while_snapshot_can_be_rebuilt(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot("0", "全部收藏", ()),
                FavoriteFolderSnapshot("9", "Reading", ()),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)
        self.assertTrue(self.page.folder_button.isEnabled())
        self.assertTrue(self.page.sort_button.isEnabled())

        self.page._on_favorites_busy_changed(True, "move_album")

        self.assertFalse(self.page.folder_button.isEnabled())
        self.assertFalse(self.page.sort_button.isEnabled())
        self.assertIn("操作完成", self.page.folder_button.toolTip())

        self.page._on_favorites_busy_changed(False, "")
        self.assertTrue(self.page.folder_button.isEnabled())
        self.assertTrue(self.page.sort_button.isEnabled())

    def test_manage_button_explicitly_tracks_hover(self):
        self.assertTrue(
            self.page.manage_folders_button.testAttribute(
                Qt.WidgetAttribute.WA_Hover
            )
        )
        self.assertTrue(self.page.manage_folders_button.hasMouseTracking())

    def test_restored_complete_snapshot_reenables_folder_controls(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot("0", "全部收藏", ()),
                FavoriteFolderSnapshot("9", "Reading", ()),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SAVED_SESSION, "saved-user")
        )
        self.page._on_favorites_busy_changed(True, "restore")
        self.page._on_favorites_busy_changed(False, "")
        self.assertFalse(self.page.folder_button.isEnabled())
        self.assertFalse(self.page.manage_folders_button.isEnabled())

        self.page._on_favorites_snapshot(snapshot)

        self.assertTrue(self.page.folder_button.isEnabled())
        self.assertTrue(self.page.manage_folders_button.isEnabled())

    def test_light_and_dark_themes_paint_the_page_and_results_background(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "Default",
                    (FavoriteItemSnapshot("1", "One item"),),
                ),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)
        previous_stylesheet = self.app.styleSheet()
        samples = {}
        try:
            for theme in (Theme.LIGHT, Theme.DARK):
                self.app.setStyleSheet(load_stylesheet(theme))
                self.app.processEvents()
                page_image = self.page.grab().toImage()
                canvas_image = self.page.results_canvas.grab().toImage()
                samples[theme] = (
                    page_image.pixelColor(
                        page_image.width() - 2,
                        page_image.height() - 2,
                    ),
                    canvas_image.pixelColor(
                        canvas_image.width() - 2,
                        canvas_image.height() - 2,
                    ),
                )
        finally:
            self.app.setStyleSheet(previous_stylesheet)
            self.app.processEvents()

        for color in samples[Theme.LIGHT]:
            self.assertGreater(color.lightness(), 200)
        for color in samples[Theme.DARK]:
            self.assertLess(color.lightness(), 60)
        self.assertNotEqual(samples[Theme.LIGHT], samples[Theme.DARK])

    def test_failed_sync_keeps_stale_cards_and_last_sync_visible(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "全部收藏",
                    (FavoriteItemSnapshot("1", "Cached item"),),
                ),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)
        previous_sync_text = self.page.last_sync_label.text()

        self.page._on_favorites_failed("unavailable", "同步失败，保留旧缓存")

        self.assertEqual(self.page.last_sync_label.text(), previous_sync_text)
        self.assertEqual(
            [card.snapshot.album_id for card in self.page.favorite_cards],
            ["1"],
        )
        self.assertTrue(self.page.favorites_error_banner.isVisible())
        self.assertIn("保留旧缓存", self.page.favorites_error_label.text())

    def test_sync_stop_and_download_reuse_existing_controllers(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "Default",
                    (FavoriteItemSnapshot("1449491", "Title"),),
                ),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)

        self.page.sync_button.click()
        self.favorites_controller.sync.assert_called_once_with("mr")
        self.page._on_favorites_busy_changed(True, "sync")
        self.assertEqual(self.page.sync_button.text(), "停止")
        self.page.sync_button.click()
        self.favorites_controller.cancel_sync.assert_called_once_with()

        self.page._on_favorites_busy_changed(False, "")
        viewed = []
        self.page.view_task_requested.connect(viewed.append)
        self.page.favorite_cards[0].action_button.click()
        self.download_controller.add_task.assert_called_once_with("1449491")
        self.assertTrue(self.page.favorite_cards[0].task_present)
        self.page.favorite_cards[0].action_button.click()
        self.assertEqual(viewed, ["1449491"])

    def test_add_in_progress_blocks_logout_and_new_sync_commands(self):
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.assertTrue(self.page.logout_button.isEnabled())
        self.assertTrue(self.page.sync_button.isEnabled())

    def test_sort_waits_for_sync_and_filter_uses_controller_result(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "全部收藏",
                    (
                        FavoriteItemSnapshot("1", "Alpha"),
                        FavoriteItemSnapshot("2", "Beta"),
                    ),
                ),
            ),
            order_by="mr",
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)
        self.favorites_controller.current_snapshot = snapshot

        target = next(
            action
            for action in self.page.sort_menu.actions()
            if action.data() == "mp"
        )
        QTimer.singleShot(
            50,
            lambda: QTest.mouseClick(
                self.page.sort_menu,
                Qt.MouseButton.LeftButton,
                pos=self.page.sort_menu.actionGeometry(target).center(),
            ),
        )
        QTest.mouseClick(
            self.page.sort_button,
            Qt.MouseButton.LeftButton,
        )
        self.app.processEvents()
        self.assertEqual(self.page.sort_button.text(), "更新时间  ▾")
        self.assertEqual(
            [
                action.data()
                for action in self.page.sort_menu.actions()
                if action.isChecked()
            ],
            ["mp"],
        )
        self.assertIn("待按更新时间同步", self.page.last_sync_label.text())
        self.favorites_controller.sync.assert_not_called()
        self.page.sync_button.click()
        self.favorites_controller.sync.assert_called_once_with("mp")

        self.page.keyword_input.setText("alpha")
        self.page._submit_filter()
        self.favorites_controller.filter_items.assert_called_with(
            "0", "alpha"
        )
        self.page._on_filter_result(
            1,
            FavoritesFilterSnapshot(
                "0",
                "alpha",
                (FavoriteItemSnapshot("1", "Alpha"),),
            ),
        )
        self.assertEqual(
            [card.snapshot.album_id for card in self.page.favorite_cards],
            ["1"],
        )
        self.assertEqual(self.page.favorites_summary.text(), "共 1 条")

    def test_card_move_uses_single_target_dialog(self):
        snapshot = FavoritesSnapshot(
            "2026-07-16T12:45:00Z",
            (
                FavoriteFolderSnapshot(
                    "0",
                    "全部收藏",
                    (FavoriteItemSnapshot("1", "Alpha"),),
                ),
                FavoriteFolderSnapshot("9", "Reading", ()),
            ),
        )
        self.page._on_snapshot(
            AccountSnapshot(AccountStatus.SIGNED_IN, "saved-user")
        )
        self.page._on_favorites_snapshot(snapshot)
        self.favorites_controller.current_snapshot = snapshot
        self.assertTrue(self.page.favorite_cards[0].move_button.isEnabled())

        with patch(
            "jm_downloader.qt.pages.favorites_page.FavoriteTargetDialog.choose",
            return_value="9",
        ):
            self.page.favorite_cards[0].move_button.click()

        self.favorites_controller.move_album.assert_called_once_with("1", "9")

        self.page._on_favorites_busy_changed(True, "add")

        self.assertFalse(self.page.logout_button.isEnabled())
        self.assertFalse(self.page.sync_button.isEnabled())
        self.assertEqual(self.page.sync_button.text(), "同步")

        self.page._on_favorites_busy_changed(False, "")
        self.assertTrue(self.page.logout_button.isEnabled())
        self.assertTrue(self.page.sync_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
