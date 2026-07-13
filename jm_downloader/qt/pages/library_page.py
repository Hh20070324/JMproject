from .base import SectionPage


class LibraryPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__("本地漫画库", "libraryPage", parent)
