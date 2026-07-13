from .base import SectionPage


class SettingsPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__("设置", "settingsPage", parent)
