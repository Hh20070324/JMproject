from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class SectionPage(QWidget):
    def __init__(self, title: str, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)

        heading = QLabel(title, self)
        heading.setObjectName("pageTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(heading)

        rule = QFrame(self)
        rule.setObjectName("pageRule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)
        layout.addWidget(rule)

        self.content = QWidget(self)
        self.content.setObjectName("pageContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(16)
        layout.addWidget(self.content, 1)
