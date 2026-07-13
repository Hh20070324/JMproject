from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
)

from ..theme import Theme, ThemeManager
from .base import SectionPage


class SettingsPage(SectionPage):
    def __init__(self, theme_manager: ThemeManager, parent=None):
        super().__init__("设置", "settingsPage", parent)
        self._theme_manager = theme_manager
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        section_title = QLabel("外观", self.content)
        section_title.setObjectName("sectionTitle")
        self.content_layout.addWidget(section_title)

        theme_row = QFrame(self.content)
        theme_row.setObjectName("settingsRow")
        row_layout = QHBoxLayout(theme_row)
        row_layout.setContentsMargins(0, 4, 0, 12)
        row_layout.setSpacing(16)

        theme_label = QLabel("主题模式", theme_row)
        theme_label.setObjectName("settingsLabel")
        row_layout.addWidget(theme_label)
        row_layout.addStretch(1)

        segment = QFrame(theme_row)
        segment.setObjectName("themeSegment")
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(3, 3, 3, 3)
        segment_layout.setSpacing(2)

        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_buttons = {}
        for index, (theme, text) in enumerate(
            ((Theme.LIGHT, "明亮"), (Theme.DARK, "黑暗"))
        ):
            button = QToolButton(segment)
            button.setObjectName("themeButton")
            button.setProperty("theme", theme.value)
            button.setText(text)
            button.setCheckable(True)
            button.setFixedSize(76, 34)
            button.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
            button.clicked.connect(
                lambda checked=False, selected=theme: (
                    self._theme_manager.set_theme(selected) if checked else None
                )
            )
            self._theme_group.addButton(button, index)
            self._theme_buttons[theme] = button
            segment_layout.addWidget(button)

        row_layout.addWidget(segment)
        self.content_layout.addWidget(theme_row)

        self._theme_manager.theme_changed.connect(self._sync_theme)
        self._sync_theme(self._theme_manager.theme.value)

    def theme_button(self, theme: Theme) -> QToolButton:
        return self._theme_buttons[theme]

    def _sync_theme(self, theme_value: str) -> None:
        theme = Theme(theme_value)
        self._theme_buttons[theme].setChecked(True)
