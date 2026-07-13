from .base import SectionPage


class DownloadPage(SectionPage):
    def __init__(self, parent=None):
        super().__init__("下载任务", "downloadPage", parent)
