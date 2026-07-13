from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class SectionPage(QWidget):
    def __init__(self, title: str, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 32, 36, 32)
        layout.setSpacing(18)

        heading = QLabel(title, self)
        heading.setObjectName("pageTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(heading)

        rule = QFrame(self)
        rule.setObjectName("pageRule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        layout.addWidget(rule)
        layout.addStretch(1)
