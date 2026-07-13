from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...settings import AppSettings, serialize_portable_path
from ..controllers.settings_controller import SettingsController
from ..theme import Theme, ThemeManager
from .base import SectionPage


class SettingsPage(SectionPage):
    def __init__(
        self,
        theme_manager: ThemeManager | None = None,
        parent=None,
        settings_controller: SettingsController | None = None,
    ):
        super().__init__("设置", "settingsPage", parent)
        self._theme_manager = theme_manager
        self._controller = settings_controller
        self._loading = False

        self.settings_scroll = QScrollArea(self.content)
        self.settings_scroll.setObjectName("settingsScrollArea")
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.settings_canvas = QWidget(self.settings_scroll)
        self.settings_canvas.setObjectName("settingsCanvas")
        canvas_layout = QVBoxLayout(self.settings_canvas)
        canvas_layout.setContentsMargins(0, 0, 8, 0)
        canvas_layout.setSpacing(20)

        self._create_storage_section(canvas_layout)
        self._create_download_section(canvas_layout)
        self._create_application_section(canvas_layout)
        canvas_layout.addStretch(1)
        self.settings_scroll.setWidget(self.settings_canvas)
        self.content_layout.addWidget(self.settings_scroll, 1)

        self._create_action_bar()
        self._connect_dirty_signals()

        if self._controller is not None:
            self._controller.settings_changed.connect(self._load_settings)
            self._controller.save_succeeded.connect(self._on_save_succeeded)
            self._controller.save_failed.connect(self._on_save_failed)
            settings = self._controller.settings
        else:
            selected_theme = (
                self._theme_manager.theme.value
                if self._theme_manager is not None
                else Theme.LIGHT.value
            )
            settings = AppSettings(theme=selected_theme)
            if self._theme_manager is not None:
                self._theme_manager.theme_changed.connect(self._sync_theme)

        self._load_settings(settings)

    def theme_button(self, theme: Theme) -> QToolButton:
        return self._theme_buttons[theme]

    def _create_storage_section(self, layout: QVBoxLayout) -> None:
        section = self._create_section(layout, "存储位置")

        self.pictures_directory_input = QLineEdit(section)
        self.pictures_directory_input.setObjectName("settingsPathInput")
        self.pictures_directory_input.setClearButtonEnabled(True)
        pictures_control = self._path_control(
            section,
            self.pictures_directory_input,
            "选择图片目录",
            lambda: self._choose_directory(self.pictures_directory_input),
        )
        self._add_row(section, "漫画图片", pictures_control)

        self.pdf_directory_input = QLineEdit(section)
        self.pdf_directory_input.setObjectName("settingsPathInput")
        self.pdf_directory_input.setClearButtonEnabled(True)
        pdf_control = self._path_control(
            section,
            self.pdf_directory_input,
            "选择 PDF 目录",
            lambda: self._choose_directory(self.pdf_directory_input),
        )
        self._add_row(section, "PDF 文件", pdf_control)

    def _create_download_section(self, layout: QVBoxLayout) -> None:
        section = self._create_section(layout, "下载性能")

        self.max_concurrent_tasks_spin = QSpinBox(section)
        self.max_concurrent_tasks_spin.setObjectName("settingsSpinBox")
        self.max_concurrent_tasks_spin.setRange(1, 8)
        self.max_concurrent_tasks_spin.setSuffix(" 个任务")
        self.max_concurrent_tasks_spin.setFixedWidth(150)
        self._add_row(section, "同时下载", self.max_concurrent_tasks_spin)

        self.image_concurrency_spin = QSpinBox(section)
        self.image_concurrency_spin.setObjectName("settingsSpinBox")
        self.image_concurrency_spin.setRange(1, 64)
        self.image_concurrency_spin.setSuffix(" 张图片")
        self.image_concurrency_spin.setFixedWidth(150)
        self._add_row(section, "图片并发", self.image_concurrency_spin)

    def _create_application_section(self, layout: QVBoxLayout) -> None:
        section = self._create_section(layout, "应用")

        self.log_level_combo = QComboBox(section)
        self.log_level_combo.setObjectName("settingsComboBox")
        for label, value in (
            ("调试", "DEBUG"),
            ("信息", "INFO"),
            ("警告", "WARNING"),
            ("错误", "ERROR"),
        ):
            self.log_level_combo.addItem(label, value)
        self.log_level_combo.setFixedWidth(150)
        self._add_row(section, "日志级别", self.log_level_combo)

        self.startup_page_combo = QComboBox(section)
        self.startup_page_combo.setObjectName("settingsComboBox")
        for label, value in (
            ("下载任务", "downloads"),
            ("本地漫画库", "library"),
            ("设置", "settings"),
        ):
            self.startup_page_combo.addItem(label, value)
        self.startup_page_combo.setFixedWidth(150)
        self._add_row(section, "启动页面", self.startup_page_combo)

        size_control = QWidget(section)
        size_control.setObjectName("settingsInlineControl")
        size_layout = QHBoxLayout(size_control)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(8)

        self.window_width_spin = QSpinBox(size_control)
        self.window_width_spin.setObjectName("settingsSpinBox")
        self.window_width_spin.setRange(760, 10000)
        self.window_width_spin.setSuffix(" px")
        self.window_width_spin.setFixedWidth(112)
        size_layout.addWidget(self.window_width_spin)

        separator = QLabel("x", size_control)
        separator.setObjectName("settingsSizeSeparator")
        separator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        size_layout.addWidget(separator)

        self.window_height_spin = QSpinBox(size_control)
        self.window_height_spin.setObjectName("settingsSpinBox")
        self.window_height_spin.setRange(520, 10000)
        self.window_height_spin.setSuffix(" px")
        self.window_height_spin.setFixedWidth(112)
        size_layout.addWidget(self.window_height_spin)
        size_layout.addStretch(1)
        self._add_row(section, "窗口尺寸", size_control)

        theme_segment = QFrame(section)
        theme_segment.setObjectName("themeSegment")
        theme_layout = QHBoxLayout(theme_segment)
        theme_layout.setContentsMargins(3, 3, 3, 3)
        theme_layout.setSpacing(2)

        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_buttons = {}
        for index, (theme, text) in enumerate(
            ((Theme.LIGHT, "明亮"), (Theme.DARK, "黑暗"))
        ):
            button = QToolButton(theme_segment)
            button.setObjectName("themeButton")
            button.setProperty("theme", theme.value)
            button.setText(text)
            button.setCheckable(True)
            button.setFixedSize(76, 34)
            button.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Fixed,
            )
            if self._controller is None and self._theme_manager is not None:
                button.clicked.connect(
                    lambda checked=False, selected=theme: (
                        self._theme_manager.set_theme(selected) if checked else None
                    )
                )
            self._theme_group.addButton(button, index)
            self._theme_buttons[theme] = button
            theme_layout.addWidget(button)
        self._add_row(section, "主题模式", theme_segment)

    def _create_action_bar(self) -> None:
        action_bar = QFrame(self.content)
        action_bar.setObjectName("settingsActionBar")
        layout = QHBoxLayout(action_bar)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(8)

        self.save_status_label = QLabel(action_bar)
        self.save_status_label.setObjectName("settingsSaveStatus")
        self.save_status_label.setWordWrap(True)
        self.save_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.save_status_label, 1)

        style = self.style()
        self.restore_defaults_button = QPushButton("恢复默认", action_bar)
        self.restore_defaults_button.setObjectName("restoreSettingsButton")
        self.restore_defaults_button.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton)
        )
        self.restore_defaults_button.setFixedSize(112, 38)
        self.restore_defaults_button.clicked.connect(self._restore_defaults)
        layout.addWidget(self.restore_defaults_button)

        self.save_button = QPushButton("保存设置", action_bar)
        self.save_button.setObjectName("saveSettingsButton")
        self.save_button.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.save_button.setFixedSize(112, 38)
        self.save_button.clicked.connect(self._save)
        layout.addWidget(self.save_button)
        self.content_layout.addWidget(action_bar)

        enabled = self._controller is not None
        self.save_button.setEnabled(enabled)
        self.restore_defaults_button.setEnabled(enabled)

    def _create_section(self, layout: QVBoxLayout, title: str) -> QFrame:
        section = QFrame(self.settings_canvas)
        section.setObjectName("settingsSection")
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(0)

        heading = QLabel(title, section)
        heading.setObjectName("sectionTitle")
        heading.setFixedHeight(36)
        section_layout.addWidget(heading)
        layout.addWidget(section)
        return section

    @staticmethod
    def _add_row(section: QFrame, label: str, control: QWidget) -> None:
        row = QFrame(section)
        row.setObjectName("settingsRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(16)

        name = QLabel(label, row)
        name.setObjectName("settingsLabel")
        name.setFixedWidth(132)
        layout.addWidget(name)
        layout.addWidget(control, 1)
        section.layout().addWidget(row)

    def _path_control(
        self,
        parent: QWidget,
        editor: QLineEdit,
        tooltip: str,
        callback,
    ) -> QWidget:
        control = QWidget(parent)
        control.setObjectName("settingsInlineControl")
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(editor, 1)

        button = QToolButton(control)
        button.setObjectName("settingsBrowseButton")
        button.setToolTip(tooltip)
        button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        button.setFixedSize(36, 36)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return control

    def _choose_directory(self, editor: QLineEdit) -> None:
        start = self._directory_start(editor.text())
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择目录",
            str(start),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        editor.setText(self._portable_directory(Path(selected)))

    def _connect_dirty_signals(self) -> None:
        self.pictures_directory_input.textChanged.connect(self._mark_dirty)
        self.pdf_directory_input.textChanged.connect(self._mark_dirty)
        self.max_concurrent_tasks_spin.valueChanged.connect(self._mark_dirty)
        self.image_concurrency_spin.valueChanged.connect(self._mark_dirty)
        self.window_width_spin.valueChanged.connect(self._mark_dirty)
        self.window_height_spin.valueChanged.connect(self._mark_dirty)
        self.log_level_combo.currentIndexChanged.connect(self._mark_dirty)
        self.startup_page_combo.currentIndexChanged.connect(self._mark_dirty)
        self._theme_group.buttonClicked.connect(self._mark_dirty)

    def _mark_dirty(self, *_args) -> None:
        if not self._loading:
            self.save_status_label.clear()

    def _directory_start(self, value: str) -> Path:
        candidate = Path(value.strip()) if value.strip() else Path()
        if not candidate.is_absolute():
            candidate = self._root_path() / candidate
        if candidate.is_dir():
            return candidate
        return self._root_path()

    def _portable_directory(self, selected: Path) -> str:
        return serialize_portable_path(self._root_path(), selected)

    def _root_path(self) -> Path:
        if self._controller is not None:
            return self._controller.root_path
        return Path.cwd().resolve()

    def _save(self) -> None:
        if self._controller is None:
            return
        self.save_status_label.clear()
        settings = self._collect_settings()
        self._set_actions_enabled(False)
        if not self._controller.save(settings):
            self._set_actions_enabled(True)

    def _restore_defaults(self) -> None:
        if self._controller is None:
            return
        answer = QMessageBox.question(
            self,
            "恢复默认设置",
            "确定恢复全部默认设置吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.save_status_label.clear()
        self._set_actions_enabled(False)
        if not self._controller.reset_defaults():
            self._set_actions_enabled(True)

    def _collect_settings(self) -> AppSettings:
        current = self._controller.settings
        checked_theme = next(
            (
                theme.value
                for theme, button in self._theme_buttons.items()
                if button.isChecked()
            ),
            Theme.LIGHT.value,
        )
        return replace(
            current,
            pictures_directory=self._normalized_directory_value(
                self.pictures_directory_input.text()
            ),
            pdf_directory=self._normalized_directory_value(
                self.pdf_directory_input.text()
            ),
            max_concurrent_tasks=self.max_concurrent_tasks_spin.value(),
            image_concurrency=self.image_concurrency_spin.value(),
            log_level=str(self.log_level_combo.currentData()),
            window_width=self.window_width_spin.value(),
            window_height=self.window_height_spin.value(),
            startup_page=str(self.startup_page_combo.currentData()),
            theme=checked_theme,
        )

    def _normalized_directory_value(self, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            return stripped
        path = Path(stripped)
        if path.is_absolute():
            return self._portable_directory(path)
        return stripped

    def _load_settings(self, settings: AppSettings) -> None:
        self._loading = True
        try:
            self.pictures_directory_input.setText(
                str(settings.pictures_directory)
            )
            self.pdf_directory_input.setText(str(settings.pdf_directory))
            self.max_concurrent_tasks_spin.setValue(settings.max_concurrent_tasks)
            self.image_concurrency_spin.setValue(settings.image_concurrency)
            self.window_width_spin.setValue(settings.window_width)
            self.window_height_spin.setValue(settings.window_height)
            self._select_combo(self.log_level_combo, settings.log_level)
            self._select_combo(self.startup_page_combo, settings.startup_page)
            self._sync_theme(settings.theme)
        finally:
            self._loading = False

    @staticmethod
    def _select_combo(combo: QComboBox, value: str) -> None:
        index = combo.findData(str(value))
        if index >= 0:
            combo.setCurrentIndex(index)

    def _sync_theme(self, theme_value: str) -> None:
        try:
            theme = theme_value if isinstance(theme_value, Theme) else Theme(theme_value)
        except (TypeError, ValueError):
            theme = Theme.LIGHT
        self._theme_buttons[theme].setChecked(True)

    def _on_save_succeeded(self, _settings: AppSettings) -> None:
        self._set_actions_enabled(True)
        self.save_status_label.setText(
            "设置已保存；部分设置重启后生效"
        )

    def _on_save_failed(self, message: str) -> None:
        self._set_actions_enabled(True)
        QMessageBox.warning(self, "设置保存失败", message)

    def _set_actions_enabled(self, enabled: bool) -> None:
        self.save_button.setEnabled(enabled)
        self.restore_defaults_button.setEnabled(enabled)
