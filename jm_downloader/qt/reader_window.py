from dataclasses import replace

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout

from .controllers.reader_controller import ReaderController
from .controllers.reader_download_controller import ReaderDownloadController
from .controllers.settings_controller import SettingsController
from .pages.reader_page import ReaderPage


class ReaderWindow(QDialog):
    """Single non-modal owner for the online reader page."""

    closed = Signal()

    def __init__(
        self,
        controller: ReaderController,
        parent=None,
        *,
        settings_controller: SettingsController | None = None,
        download_state_controller: ReaderDownloadController | None = None,
        persist_geometry: bool = True,
    ):
        super().__init__(parent, Qt.WindowType.Window)
        self.controller = controller
        self.settings_controller = settings_controller
        self._persist_geometry = bool(persist_geometry)
        self._session_album_id: str | None = None
        self._geometry_restored = False
        self.setObjectName("readerWindow")
        self.setWindowTitle("在线阅读")
        self.setMinimumSize(760, 520)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.page = ReaderPage(
            controller,
            self,
            settings_controller=settings_controller,
            download_state_controller=download_state_controller,
        )
        layout.addWidget(self.page)
        self.page.back_requested.connect(self.close)
        self._shortcuts = []
        for sequence in ("Left", "PgUp", "Shift+Space"):
            self._add_shortcut(sequence, self.page.previous_page)
        for sequence in ("Right", "PgDown", "Space"):
            self._add_shortcut(sequence, self.page.next_page)

    @property
    def session_album_id(self) -> str | None:
        return self._session_album_id

    @property
    def has_session(self) -> bool:
        return self._session_album_id is not None and self.isVisible()

    def begin_session(self, album_id: str, title: str | None = None) -> None:
        self._session_album_id = str(album_id)
        self.setWindowTitle(
            f"{title.strip()} - 在线阅读"
            if isinstance(title, str) and title.strip()
            else f"JM {album_id} - 在线阅读"
        )
        if not self._geometry_restored:
            self._restore_saved_geometry()
            self._geometry_restored = True
        self._show_and_activate()

    def activate_session(self) -> None:
        self._show_and_activate()

    def end_session(self) -> None:
        if self._session_album_id is None:
            return
        generation = self.controller.leave()
        self.page.end_session(generation)
        self._session_album_id = None
        self.setWindowTitle("在线阅读")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_geometry()
        self.end_session()
        event.accept()
        self.closed.emit()

    def reject(self) -> None:
        """Keep QDialog's implicit Escape action from hiding the reader."""
        return

    def _show_and_activate(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _add_shortcut(self, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(callback)
        self._shortcuts.append(shortcut)

    def _restore_saved_geometry(self) -> None:
        settings = (
            self.settings_controller.settings
            if self.settings_controller is not None
            else None
        )
        width = (
            settings.reader_window_width
            if settings is not None
            else 1000
        )
        height = (
            settings.reader_window_height
            if settings is not None
            else 760
        )
        saved_x = settings.reader_window_x if settings is not None else None
        saved_y = settings.reader_window_y if settings is not None else None
        screen = self._target_screen(saved_x, saved_y, width, height)
        if screen is None:
            self.resize(width, height)
            return
        available = screen.availableGeometry()
        width = min(max(self.minimumWidth(), width), available.width())
        height = min(max(self.minimumHeight(), height), available.height())
        if saved_x is None or saved_y is None:
            x = available.center().x() - width // 2
            y = available.center().y() - height // 2
        else:
            x = saved_x
            y = saved_y
        x = min(max(x, available.left()), available.right() - width + 1)
        y = min(max(y, available.top()), available.bottom() - height + 1)
        self.setGeometry(x, y, width, height)

    def _target_screen(
        self,
        x: int | None,
        y: int | None,
        width: int,
        height: int,
    ):
        screens = tuple(QApplication.screens())
        if x is not None and y is not None:
            saved = QRect(x, y, width, height)
            candidates = [
                (
                    saved.intersected(screen.availableGeometry()).width()
                    * saved.intersected(
                        screen.availableGeometry()
                    ).height(),
                    screen,
                )
                for screen in screens
            ]
            visible = [candidate for candidate in candidates if candidate[0] > 0]
            if visible:
                return max(visible, key=lambda candidate: candidate[0])[1]
        parent = self.parentWidget()
        parent_screen = parent.screen() if parent is not None else None
        return parent_screen or QApplication.primaryScreen()

    def _save_geometry(self) -> None:
        if (
            not self._persist_geometry
            or self.settings_controller is None
            or not self._geometry_restored
        ):
            return
        rect = self.normalGeometry() if self.isMaximized() else self.geometry()
        current = self.settings_controller.settings
        updated = replace(
            current,
            reader_window_width=max(self.minimumWidth(), rect.width()),
            reader_window_height=max(self.minimumHeight(), rect.height()),
            reader_window_x=rect.x(),
            reader_window_y=rect.y(),
        )
        if updated != current:
            self.settings_controller.save(updated)


__all__ = ["ReaderWindow"]
