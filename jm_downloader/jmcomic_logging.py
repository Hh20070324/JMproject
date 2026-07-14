import logging
import threading

import jmcomic


LOGGER = logging.getLogger("jm-downloader")
_INSTALL_LOCK = threading.Lock()

_SAFE_EVENTS = {
    "album.before": (logging.DEBUG, "JM album download started"),
    "album.after": (logging.DEBUG, "JM album download finished"),
    "photo.before": (logging.DEBUG, "JM chapter download started"),
    "photo.after": (logging.DEBUG, "JM chapter download finished"),
    "image.before": (logging.DEBUG, "JM image download started"),
    "image.after": (logging.DEBUG, "JM image download finished"),
    "api.update_domain.success": (
        logging.INFO,
        "JM API domain discovery succeeded",
    ),
    "api.update_domain.empty": (
        logging.WARNING,
        "JM API domain discovery returned no domains",
    ),
    "api.update_domain.error": (
        logging.WARNING,
        "JM API domain discovery endpoint failed",
    ),
    "req.retry": (logging.WARNING, "JM request retrying"),
    "req.error": (logging.WARNING, "JM request attempt failed"),
    "req.fallback": (logging.ERROR, "JM request attempts exhausted"),
    "image.failed": (logging.ERROR, "JM image download failed"),
    "photo.failed": (logging.ERROR, "JM chapter download failed"),
    "downloader.feature.exception": (
        logging.ERROR,
        "JM downloader feature failed",
    ),
    "dler.exception": (logging.ERROR, "JM downloader callback failed"),
}


def _safe_jmcomic_log(topic, message, error=None) -> None:
    if not isinstance(topic, str):
        return
    event = _SAFE_EVENTS.get(topic)
    if event is None:
        return

    level, safe_message = event
    if error is None and isinstance(message, BaseException):
        error = message

    if error is None:
        LOGGER.log(level, safe_message)
        return

    LOGGER.log(level, "%s (%s)", safe_message, type(error).__name__)


def install_safe_jmcomic_logging() -> None:
    """Install the process-wide safe JMComic log bridge before client use."""
    with _INSTALL_LOCK:
        jmcomic.JmModuleConfig.EXECUTOR_LOG = _safe_jmcomic_log
        jmcomic.JmModuleConfig.FLAG_DUMP_HTML_ON_REGEX_ERROR = False


__all__ = ["install_safe_jmcomic_logging"]
