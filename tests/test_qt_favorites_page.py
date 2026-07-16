import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from jm_downloader.account import AccountService, AccountStore
from jm_downloader.models import AccountSnapshot, AccountStatus
from jm_downloader.protected_store import ProtectedStore
from jm_downloader.qt.controllers.account_controller import AccountController
from jm_downloader.qt.pages.favorites_page import FavoritesPage
from jm_downloader.settings import AppPaths
from tests.account_fakes import FakeJmAccountClient


class PageProtector:
    def protect(self, plaintext):
        return b"page\0" + plaintext[::-1]

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"page\0"):
            raise ValueError("invalid")
        return ciphertext[len(b"page\0") :][::-1]


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
        self.page = FavoritesPage(self.controller)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.resize(552, 520)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.controller.dispose()
        self.controller.deleteLater()
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
            self.page.login_state,
        )
        self.assertEqual(self.page.login_button.text(), "重新登录")
        self.assertEqual(self.page.username_input.text(), "saved-user")

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


if __name__ == "__main__":
    unittest.main()
