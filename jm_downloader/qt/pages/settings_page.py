from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...settings import AppSettings, serialize_portable_path
from ...option_config import API_ROUTE_LABELS
from ..controllers.settings_controller import SettingsController
from ..icons import svg_icon
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
        self._create_theme_section(canvas_layout)
        canvas_layout.addStretch(1)
        self.settings_scroll.setWidget(self.settings_canvas)
        self.content_layout.addWidget(self.settings_scroll, 1)

        self._create_action_bar()
        self._connect_dirty_signals()

        if self._controller is not None:
            self._controller.settings_changed.connect(self._load_settings)
            self._controller.save_succeeded.connect(self._on_save_succeeded)
            self._controller.save_failed.connect(self._on_save_failed)
            self._controller.route_test_succeeded.connect(
                self._on_route_test_succeeded
            )
            self._controller.route_test_failed.connect(
                self._on_route_test_failed
            )
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

        engine_control = QWidget(section)
        engine_layout = QHBoxLayout(engine_control)
        engine_layout.setContentsMargins(0, 0, 0, 0)
        self.download_engine_button = QToolButton(engine_control)
        self.download_engine_button.setObjectName("downloadEngineButton")
        self.download_engine_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.download_engine_button.setFixedSize(220, 36)
        self.download_engine_menu = QMenu(self.download_engine_button)
        self._download_engine_group = QActionGroup(self)
        self._download_engine_group.setExclusive(True)
        self._download_engine_actions = {}
        for label, value in (
            ("异步下载（推荐）", "async"),
            ("同步线程（兼容）", "sync"),
        ):
            action = self.download_engine_menu.addAction(label)
            action.setData(value)
            action.setCheckable(True)
            self._download_engine_group.addAction(action)
            self._download_engine_actions[value] = action
        self._download_engine_group.triggered.connect(
            self._select_download_engine
        )
        self.download_engine_button.setMenu(self.download_engine_menu)
        engine_layout.addWidget(self.download_engine_button)
        engine_layout.addStretch(1)
        self._add_row(section, "下载引擎", engine_control)

        route_control = QWidget(section)
        route_layout = QHBoxLayout(route_control)
        route_layout.setContentsMargins(0, 0, 0, 0)
        route_layout.setSpacing(8)
        self.api_route_button = QToolButton(route_control)
        self.api_route_button.setObjectName("apiRouteButton")
        self.api_route_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.api_route_button.setFixedSize(220, 36)
        self.api_route_menu = QMenu(self.api_route_button)
        self._api_route_group = QActionGroup(self)
        self._api_route_group.setExclusive(True)
        self._api_route_actions = {}
        for value, label in API_ROUTE_LABELS.items():
            action = self.api_route_menu.addAction(label)
            action.setData(value)
            action.setCheckable(True)
            self._api_route_group.addAction(action)
            self._api_route_actions[value] = action
        self._api_route_group.triggered.connect(self._select_api_route)
        self.api_route_button.setMenu(self.api_route_menu)
        route_layout.addWidget(self.api_route_button)
        self.test_api_route_button = QPushButton("测试路线", route_control)
        self.test_api_route_button.setObjectName("testApiRouteButton")
        self.test_api_route_button.setFixedSize(100, 36)
        self.test_api_route_button.clicked.connect(self._test_api_route)
        self.test_api_route_button.setEnabled(self._controller is not None)
        route_layout.addWidget(self.test_api_route_button)
        route_layout.addStretch(1)
        self._add_row(section, "API 路线", route_control)

        package_control = QWidget(section)
        package_layout = QHBoxLayout(package_control)
        package_layout.setContentsMargins(0, 0, 0, 0)
        self.package_format_button = QToolButton(package_control)
        self.package_format_button.setObjectName("packageFormatButton")
        self.package_format_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.package_format_button.setFixedSize(220, 36)
        self.package_format_menu = QMenu(self.package_format_button)
        self._package_format_group = QActionGroup(self)
        self._package_format_group.setExclusive(True)
        self._package_format_actions = {}
        for label, value in (
            ("分章 PDF", "pdf"),
            ("分章 CBZ", "cbz"),
            ("仅图片", "images"),
        ):
            action = self.package_format_menu.addAction(label)
            action.setData(value)
            action.setCheckable(True)
            self._package_format_group.addAction(action)
            self._package_format_actions[value] = action
        self._package_format_group.triggered.connect(
            self._select_package_format
        )
        self.package_format_button.setMenu(self.package_format_menu)
        package_layout.addWidget(self.package_format_button)
        package_layout.addStretch(1)
        self._add_row(section, "保存方式", package_control)

        image_format_control = QWidget(section)
        image_format_layout = QHBoxLayout(image_format_control)
        image_format_layout.setContentsMargins(0, 0, 0, 0)
        self.image_format_button = QToolButton(image_format_control)
        self.image_format_button.setObjectName("imageFormatButton")
        self.image_format_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.image_format_button.setFixedSize(220, 36)
        self.image_format_menu = QMenu(self.image_format_button)
        self._image_format_group = QActionGroup(self)
        self._image_format_group.setExclusive(True)
        self._image_format_actions = {}
        for label, value in (("JPG", "jpg"), ("PNG", "png")):
            action = self.image_format_menu.addAction(label)
            action.setData(value)
            action.setCheckable(True)
            self._image_format_group.addAction(action)
            self._image_format_actions[value] = action
        self._image_format_group.triggered.connect(
            self._select_image_format
        )
        self.image_format_button.setMenu(self.image_format_menu)
        image_format_layout.addWidget(self.image_format_button)
        image_format_layout.addStretch(1)
        self._add_row(section, "图片格式", image_format_control)

        self.max_concurrent_tasks_spin = QSpinBox(section)
        self.max_concurrent_tasks_spin.setObjectName("settingsSpinBox")
        self.max_concurrent_tasks_spin.setRange(1, 8)
        self.max_concurrent_tasks_spin.setSuffix(" 个任务")
        tasks_control = self._stepper_control(
            section,
            self.max_concurrent_tasks_spin,
            "maxConcurrentTasks",
        )
        self._add_row(section, "同时下载", tasks_control)

        self.image_concurrency_spin = QSpinBox(section)
        self.image_concurrency_spin.setObjectName("settingsSpinBox")
        self.image_concurrency_spin.setRange(1, 64)
        self.image_concurrency_spin.setSuffix(" 张图片")
        images_control = self._stepper_control(
            section,
            self.image_concurrency_spin,
            "imageConcurrency",
        )
        self._add_row(section, "图片并发", images_control)

        behavior_control = QWidget(section)
        behavior_control.setObjectName("settingsComboControl")
        behavior_layout = QHBoxLayout(behavior_control)
        behavior_layout.setContentsMargins(0, 0, 0, 0)
        behavior_layout.setSpacing(0)

        self.multi_chapter_behavior_button = QToolButton(behavior_control)
        self.multi_chapter_behavior_button.setObjectName(
            "multiChapterBehaviorButton"
        )
        self.multi_chapter_behavior_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.multi_chapter_behavior_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.multi_chapter_behavior_button.setFixedSize(220, 36)
        self.multi_chapter_behavior_button.setToolTip(
            "选择多章漫画的章节下载方式"
        )

        self.multi_chapter_behavior_menu = QMenu(
            self.multi_chapter_behavior_button
        )
        self.multi_chapter_behavior_menu.setObjectName(
            "multiChapterBehaviorMenu"
        )
        self._multi_chapter_behavior_group = QActionGroup(self)
        self._multi_chapter_behavior_group.setExclusive(True)
        self._multi_chapter_behavior_actions: dict[str, QAction] = {}
        for label, behavior in (
            ("并行下载（同时 2 章）", "parallel"),
            ("排队下载（同时 1 章）", "queued"),
        ):
            action = self.multi_chapter_behavior_menu.addAction(label)
            action.setData(behavior)
            action.setCheckable(True)
            self._multi_chapter_behavior_group.addAction(action)
            self._multi_chapter_behavior_actions[behavior] = action
        self._multi_chapter_behavior_group.triggered.connect(
            self._select_multi_chapter_behavior
        )
        self.multi_chapter_behavior_button.setMenu(
            self.multi_chapter_behavior_menu
        )
        behavior_layout.addWidget(self.multi_chapter_behavior_button)
        behavior_layout.addStretch(1)
        self._add_row(section, "多章漫画下载行为", behavior_control)

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
        log_level_control = self._combo_control(
            section,
            self.log_level_combo,
            "logLevel",
            "展开日志级别",
        )
        self._add_row(section, "日志级别", log_level_control)

        self.startup_page_combo = QComboBox(section)
        self.startup_page_combo.setObjectName("settingsComboBox")
        for label, value in (
            ("搜索与下载", "downloads"),
            ("我的收藏", "favorites"),
            ("本地漫画库", "library"),
            ("设置", "settings"),
        ):
            self.startup_page_combo.addItem(label, value)
        startup_page_control = self._combo_control(
            section,
            self.startup_page_combo,
            "startupPage",
            "展开启动页面",
        )
        self._add_row(section, "启动页面", startup_page_control)

        size_control = QWidget(section)
        size_control.setObjectName("settingsInlineControl")
        size_layout = QHBoxLayout(size_control)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(8)

        self.window_width_spin = QSpinBox(size_control)
        self.window_width_spin.setObjectName("settingsSpinBox")
        self.window_width_spin.setRange(760, 10000)
        self.window_width_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
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
        self.window_height_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.window_height_spin.setSuffix(" px")
        self.window_height_spin.setFixedWidth(112)
        size_layout.addWidget(self.window_height_spin)
        size_layout.addStretch(1)
        self._add_row(section, "窗口尺寸", size_control)

    def _create_theme_section(self, layout: QVBoxLayout) -> None:
        section = self._create_section(layout, "主题模式")

        theme_control = QWidget(section)
        theme_control.setObjectName("settingsThemeControl")
        theme_control.setFixedHeight(36)
        theme_layout = QHBoxLayout(theme_control)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        theme_layout.setSpacing(8)

        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_buttons = {}
        for index, (theme, text) in enumerate(
            ((Theme.LIGHT, "明亮"), (Theme.DARK, "黑暗"))
        ):
            button = QToolButton(theme_control)
            button.setObjectName("themeButton")
            button.setProperty("theme", theme.value)
            button.setText(text)
            button.setIcon(svg_icon(f"{theme.value}-mode"))
            button.setToolTip(f"使用{text}主题")
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            button.setCheckable(True)
            button.setFixedSize(92, 36)
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
            theme_layout.addWidget(
                button,
                0,
                Qt.AlignmentFlag.AlignVCenter,
            )
        theme_layout.addStretch(1)
        self._add_row(section, "明暗切换", theme_control)

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

        self.restore_defaults_button = QPushButton("恢复默认", action_bar)
        self.restore_defaults_button.setObjectName("restoreSettingsButton")
        self.restore_defaults_button.setIcon(svg_icon("refresh"))
        self.restore_defaults_button.setFixedSize(112, 38)
        self.restore_defaults_button.clicked.connect(self._restore_defaults)
        layout.addWidget(self.restore_defaults_button)

        self.save_button = QPushButton("保存设置", action_bar)
        self.save_button.setObjectName("saveSettingsButton")
        self.save_button.setIcon(svg_icon("save"))
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
        button.setIcon(svg_icon("folder"))
        button.setFixedSize(36, 36)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return control

    def _combo_control(
        self,
        parent: QWidget,
        combo: QComboBox,
        name: str,
        tooltip: str,
    ) -> QWidget:
        control = QWidget(parent)
        control.setObjectName("settingsComboControl")
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        combo.setFixedWidth(150)
        layout.addWidget(combo)

        popup = QToolButton(control)
        popup.setObjectName("settingsComboButton")
        popup.setProperty("action", "expand")
        popup.setToolTip(tooltip)
        popup.setIcon(svg_icon("arrow-down"))
        popup.setFixedSize(28, 28)
        popup.clicked.connect(
            lambda checked=False, target=combo: target.showPopup()
        )
        setattr(self, f"{name}_popup_button", popup)

        layout.addWidget(popup)
        layout.addStretch(1)
        return control

    def _stepper_control(
        self,
        parent: QWidget,
        spin: QSpinBox,
        name: str,
    ) -> QWidget:
        control = QWidget(parent)
        control.setObjectName("settingsStepper")
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setFixedWidth(112)

        decrease = QToolButton(control)
        decrease.setObjectName("settingsStepButton")
        decrease.setProperty("action", "decrease")
        decrease.setToolTip("减少")
        decrease.setIcon(svg_icon("minus"))
        decrease.setFixedSize(28, 28)
        decrease.clicked.connect(spin.stepDown)

        increase = QToolButton(control)
        increase.setObjectName("settingsStepButton")
        increase.setProperty("action", "increase")
        increase.setToolTip("增加")
        increase.setIcon(svg_icon("plus"))
        increase.setFixedSize(28, 28)
        increase.clicked.connect(spin.stepUp)

        def update_buttons(value: int) -> None:
            decrease.setEnabled(value > spin.minimum())
            increase.setEnabled(value < spin.maximum())

        spin.valueChanged.connect(update_buttons)
        update_buttons(spin.value())
        setattr(self, f"{name}_decrease_button", decrease)
        setattr(self, f"{name}_increase_button", increase)

        layout.addWidget(decrease)
        layout.addWidget(spin)
        layout.addWidget(increase)
        layout.addStretch(1)
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
        self._multi_chapter_behavior_group.triggered.connect(self._mark_dirty)
        self._download_engine_group.triggered.connect(self._mark_dirty)
        self._api_route_group.triggered.connect(self._mark_dirty)
        self._package_format_group.triggered.connect(self._mark_dirty)
        self._image_format_group.triggered.connect(self._mark_dirty)
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
            multi_chapter_download_behavior=(
                self._selected_multi_chapter_behavior()
            ),
            download_engine=self._selected_download_engine(),
            api_route=self._selected_api_route(),
            download_package_format=self._selected_package_format(),
            download_image_format=self._selected_image_format(),
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
            self._set_multi_chapter_behavior(
                settings.multi_chapter_download_behavior
            )
            self._set_download_engine(settings.download_engine)
            self._set_api_route(settings.api_route)
            self._set_package_format(settings.download_package_format)
            self._set_image_format(settings.download_image_format)
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

    def _select_multi_chapter_behavior(self, action: QAction) -> None:
        self._set_multi_chapter_behavior(str(action.data()))

    def _set_multi_chapter_behavior(self, behavior: str) -> None:
        action = self._multi_chapter_behavior_actions.get(
            behavior,
            self._multi_chapter_behavior_actions["parallel"],
        )
        action.setChecked(True)
        self.multi_chapter_behavior_button.setText(f"{action.text()} ▾")

    def _selected_multi_chapter_behavior(self) -> str:
        checked = self._multi_chapter_behavior_group.checkedAction()
        if checked is None:
            return "parallel"
        return str(checked.data())

    def _select_download_engine(self, action: QAction) -> None:
        self._set_download_engine(str(action.data()))

    def _set_download_engine(self, engine: str) -> None:
        action = self._download_engine_actions.get(
            engine,
            self._download_engine_actions["async"],
        )
        action.setChecked(True)
        self.download_engine_button.setText(f"{action.text()} ▾")

    def _selected_download_engine(self) -> str:
        checked = self._download_engine_group.checkedAction()
        if checked is None:
            return "async"
        return str(checked.data())

    def _select_api_route(self, action: QAction) -> None:
        self._set_api_route(str(action.data()))

    def _set_api_route(self, route: str) -> None:
        action = self._api_route_actions.get(
            route,
            self._api_route_actions["auto"],
        )
        action.setChecked(True)
        self.api_route_button.setText(f"{action.text()} ▾")

    def _selected_api_route(self) -> str:
        checked = self._api_route_group.checkedAction()
        if checked is None:
            return "auto"
        return str(checked.data())

    def _test_api_route(self) -> None:
        if self._controller is None:
            return
        self.test_api_route_button.setEnabled(False)
        self.save_status_label.setText("正在测试路线…")
        self._controller.test_api_route(self._selected_api_route())

    def _on_route_test_succeeded(self, route: str, elapsed_ms: int) -> None:
        self.test_api_route_button.setEnabled(True)
        label = API_ROUTE_LABELS.get(route, route)
        self.save_status_label.setText(
            f"{label}可用，响应约 {elapsed_ms} ms"
        )

    def _on_route_test_failed(self, _route: str, message: str) -> None:
        self.test_api_route_button.setEnabled(True)
        self.save_status_label.setText(message)

    def _select_package_format(self, action: QAction) -> None:
        self._set_package_format(str(action.data()))

    def _set_package_format(self, value: str) -> None:
        action = self._package_format_actions.get(
            value,
            self._package_format_actions["pdf"],
        )
        action.setChecked(True)
        self.package_format_button.setText(f"{action.text()} ▾")

    def _selected_package_format(self) -> str:
        checked = self._package_format_group.checkedAction()
        return str(checked.data()) if checked is not None else "pdf"

    def _select_image_format(self, action: QAction) -> None:
        self._set_image_format(str(action.data()))

    def _set_image_format(self, value: str) -> None:
        action = self._image_format_actions.get(
            value,
            self._image_format_actions["jpg"],
        )
        action.setChecked(True)
        self.image_format_button.setText(f"{action.text()} ▾")

    def _selected_image_format(self) -> str:
        checked = self._image_format_group.checkedAction()
        return str(checked.data()) if checked is not None else "jpg"

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
