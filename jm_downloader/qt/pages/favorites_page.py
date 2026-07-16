from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...models import AccountSnapshot, AccountStatus
from ..controllers.account_controller import AccountController
from ..icons import svg_icon
from .base import SectionPage


class FavoritesPage(SectionPage):
    def __init__(
        self,
        controller: AccountController | None = None,
        parent=None,
    ):
        super().__init__("我的收藏", "favoritesPage", parent)
        self.controller = controller
        self._snapshot = (
            controller.current_snapshot
            if controller is not None
            else AccountSnapshot(AccountStatus.SIGNED_OUT)
        )
        self._busy = bool(controller is not None and controller.is_busy)

        self.state_stack = QStackedWidget(self.content)
        self.state_stack.setObjectName("accountStateStack")
        self.loading_state = self._create_loading_state()
        self.login_state = self._create_login_state()
        self.account_state = self._create_account_state()
        for widget in (
            self.loading_state,
            self.login_state,
            self.account_state,
        ):
            self.state_stack.addWidget(widget)
        self.content_layout.addWidget(self.state_stack, 1)

        if controller is not None:
            controller.snapshot_changed.connect(self._on_snapshot)
            controller.busy_changed.connect(self._on_busy_changed)
            controller.operation_failed.connect(self._on_operation_failed)
        self._render()

    def _create_loading_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("accountLoadingState")
        layout = QVBoxLayout(state)
        layout.setContentsMargins(0, 72, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        title = QLabel("正在读取本地登录信息", state)
        title.setObjectName("accountStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        detail = QLabel("此过程不会连接网络", state)
        detail.setObjectName("accountStateDetail")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(detail)
        return state

    def _create_login_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("accountLoginState")
        outer = QVBoxLayout(state)
        outer.setContentsMargins(0, 42, 0, 0)
        outer.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        form = QWidget(state)
        form.setObjectName("accountLoginForm")
        form.setMaximumWidth(460)
        form.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout = QVBoxLayout(form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.login_title = QLabel("登录账号", form)
        self.login_title.setObjectName("accountStateTitle")
        layout.addWidget(self.login_title)

        self.login_detail = QLabel("登录信息将加密保存在程序目录", form)
        self.login_detail.setObjectName("accountStateDetail")
        self.login_detail.setWordWrap(True)
        layout.addWidget(self.login_detail)

        self.username_input = QLineEdit(form)
        self.username_input.setObjectName("accountUsernameInput")
        self.username_input.setPlaceholderText("账号")
        self.username_input.setClearButtonEnabled(True)
        self.username_input.setMaxLength(128)
        self.username_input.setFixedHeight(40)
        layout.addWidget(self.username_input)

        self.password_input = QLineEdit(form)
        self.password_input.setObjectName("accountPasswordInput")
        self.password_input.setPlaceholderText("密码")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setMaxLength(512)
        self.password_input.setFixedHeight(40)
        self.password_input.returnPressed.connect(self._submit_login)
        layout.addWidget(self.password_input)

        self.login_error = QLabel("", form)
        self.login_error.setObjectName("accountErrorLabel")
        self.login_error.setWordWrap(True)
        self.login_error.setVisible(False)
        layout.addWidget(self.login_error)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 4, 0, 0)
        actions.setSpacing(8)
        self.clear_button = QPushButton("清除本地账号数据", form)
        self.clear_button.setObjectName("accountSecondaryButton")
        self.clear_button.clicked.connect(self._confirm_clear_local_data)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        self.login_button = QPushButton("登录", form)
        self.login_button.setObjectName("accountPrimaryButton")
        self.login_button.setIcon(svg_icon("user-check"))
        self.login_button.setFixedHeight(38)
        self.login_button.clicked.connect(self._submit_login)
        actions.addWidget(self.login_button)
        layout.addLayout(actions)

        outer.addWidget(form)
        return state

    def _create_account_state(self) -> QWidget:
        state = QWidget(self)
        state.setObjectName("accountSignedInState")
        layout = QVBoxLayout(state)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(14)

        summary = QFrame(state)
        summary.setObjectName("accountSummary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(18, 14, 14, 14)
        summary_layout.setSpacing(12)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(3)
        self.account_name = QLabel("", summary)
        self.account_name.setObjectName("accountName")
        text_layout.addWidget(self.account_name)
        self.account_status = QLabel("", summary)
        self.account_status.setObjectName("accountStateDetail")
        text_layout.addWidget(self.account_status)
        summary_layout.addLayout(text_layout, 1)

        self.logout_button = QToolButton(summary)
        self.logout_button.setObjectName("accountLogoutButton")
        self.logout_button.setText("退出登录")
        self.logout_button.setIcon(svg_icon("user-delete"))
        self.logout_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.logout_button.setFixedSize(104, 36)
        self.logout_button.clicked.connect(self._confirm_logout)
        summary_layout.addWidget(self.logout_button)
        layout.addWidget(summary)

        self.account_notice = QLabel("账号登录状态已就绪", state)
        self.account_notice.setObjectName("accountNotice")
        self.account_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.account_notice, 1)
        return state

    @Slot()
    def _submit_login(self) -> None:
        password = self.password_input.text()
        self.password_input.clear()
        self._set_error("")
        if self.controller is None:
            self._set_error("账号服务未初始化")
            return
        self.controller.login(self.username_input.text(), password)
        password = ""

    @Slot()
    def _confirm_logout(self) -> None:
        if self.controller is None:
            return
        answer = QMessageBox.question(
            self,
            "退出登录",
            "退出后将删除程序目录中的本地账号信息和收藏缓存，确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.logout()

    @Slot()
    def _confirm_clear_local_data(self) -> None:
        if self.controller is None:
            return
        answer = QMessageBox.question(
            self,
            "清除本地账号数据",
            "将删除 account.dat 和 favorites.dat，原文件不会自动备份。确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.logout()

    @Slot(object)
    def _on_snapshot(self, snapshot: AccountSnapshot) -> None:
        if not isinstance(snapshot, AccountSnapshot):
            return
        self._snapshot = snapshot
        self._set_error("")
        self._render()

    @Slot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._render_controls()

    @Slot(str, str)
    def _on_operation_failed(self, _code: str, message: str) -> None:
        self._set_error(message)

    def _render(self) -> None:
        status = self._snapshot.status
        if status is AccountStatus.RESTORING:
            self.state_stack.setCurrentWidget(self.loading_state)
        elif status in {AccountStatus.SAVED_SESSION, AccountStatus.SIGNED_IN}:
            self.account_name.setText(self._snapshot.username or "已登录账号")
            if status is AccountStatus.SAVED_SESSION:
                self.account_status.setText("本地会话已恢复，尚未联网验证")
            else:
                self.account_status.setText("本次登录已验证")
            self.state_stack.setCurrentWidget(self.account_state)
        else:
            if status is AccountStatus.EXPIRED:
                self.login_title.setText("登录已过期")
                self.login_detail.setText("请重新输入账号和密码")
                self.login_button.setText("重新登录")
                if self._snapshot.username:
                    self.username_input.setText(self._snapshot.username)
            elif status is AccountStatus.LOCAL_DATA_UNREADABLE:
                self.login_title.setText("本地登录信息无法读取")
                self.login_detail.setText("可以重新登录覆盖原文件，或清除本地数据")
                self.login_button.setText("重新登录")
            else:
                self.login_title.setText("登录账号")
                self.login_detail.setText("登录信息将加密保存在程序目录")
                self.login_button.setText("登录")
            self.clear_button.setVisible(
                status is AccountStatus.LOCAL_DATA_UNREADABLE
            )
            self.state_stack.setCurrentWidget(self.login_state)
        self._render_controls()

    def _render_controls(self) -> None:
        enabled = self.controller is not None and not self._busy
        self.username_input.setEnabled(enabled)
        self.password_input.setEnabled(enabled)
        self.login_button.setEnabled(enabled)
        self.clear_button.setEnabled(enabled)
        self.logout_button.setEnabled(enabled)
        if self._snapshot.status is AccountStatus.SIGNING_IN and self._busy:
            self.login_button.setText("登录中")

    def _set_error(self, message: str) -> None:
        message = str(message).strip()
        self.login_error.setText(message)
        self.login_error.setVisible(bool(message))


__all__ = ["FavoritesPage"]
