import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .jmcomic_logging import install_safe_jmcomic_logging


_CLIENT_CONSTRUCTION_LOCK = threading.RLock()


@contextmanager
def serialized_client_construction() -> Iterator[None]:
    """Serialize JMComic client construction without locking client use."""
    install_safe_jmcomic_logging()
    with _CLIENT_CONSTRUCTION_LOCK:
        yield


__all__ = ["serialized_client_construction"]
