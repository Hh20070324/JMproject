import threading


class ControlledWorker:
    """Deterministic worker used by task persistence and recovery tests."""

    instances = []

    def __init__(self, album_id, paths, **callbacks):
        self.album_id = str(album_id)
        self.paths = paths
        self.callbacks = callbacks
        self.started = False
        self.stopped = False
        self.wait_timeout = None
        self.finished = threading.Event()
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self, timeout):
        self.wait_timeout = timeout
        return self.finished.wait(timeout)

    def finish(self):
        self.finished.set()

    def emit_info(self, title="测试漫画", cover=None):
        self.callbacks["on_info"](self.album_id, title, cover)

    def emit_progress(self, percent, chapter="第一章", page="1/1"):
        self.callbacks["on_progress"](
            self.album_id,
            percent,
            chapter,
            page,
        )

    def emit_error(self, message="下载失败"):
        self.callbacks["on_error"](self.album_id, message)
        self.finish()

    def emit_complete(self, pdf_path):
        self.callbacks["on_complete"](self.album_id, str(pdf_path))
        self.finish()

    def emit_stopped(self):
        self.callbacks["on_stopped"](self.album_id)
        self.finish()
