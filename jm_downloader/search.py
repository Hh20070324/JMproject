import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import jmcomic
from curl_cffi.requests.exceptions import RequestException

from .jmcomic_logging import install_safe_jmcomic_logging
from .models import (
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from .settings import AppPaths, DEFAULT_PATHS
from .tasks import InvalidAlbumId, normalize_album_id


LOGGER = logging.getLogger("jm-downloader")
SEARCH_TIMEOUT_SECONDS = 8
SEARCH_REQUEST_RETRIES = 1
MAX_COVER_BYTES = 8 * 1024 * 1024
_CLIENT_BUILD_LOCK = threading.Lock()


class SearchError(Exception):
    code = "unknown"
    default_message = "搜索暂时失败，请稍后重试"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)


class SearchValidationError(SearchError):
    code = "validation"
    default_message = "搜索条件无效"


class SearchRejected(SearchError):
    code = "rejected"
    default_message = "上游服务拒绝了该查询"


class SearchNotFound(SearchError):
    code = "not_found"
    default_message = "未找到对应漫画"


class SearchUnavailable(SearchError):
    code = "unavailable"
    default_message = "网络暂不可用，请稍后重试"


class SearchResponseError(SearchError):
    code = "invalid_response"
    default_message = "上游响应暂时无法解析"


def normalize_search_request(request: SearchRequest) -> SearchRequest:
    if not isinstance(request, SearchRequest):
        raise SearchValidationError()
    if not isinstance(request.mode, SearchMode):
        raise SearchValidationError("搜索模式无效")
    if not isinstance(request.query, str):
        raise SearchValidationError("搜索内容无效")
    if type(request.page) is not int or request.page < 1:
        raise SearchValidationError("页码必须是正整数")

    query = request.query.strip()
    if request.mode is SearchMode.EXACT_ID:
        if request.page != 1:
            raise SearchValidationError("精确 JM 查询只支持第 1 页")
        try:
            query = _normalize_search_album_id(query)
        except InvalidAlbumId as error:
            raise SearchValidationError(str(error)) from None
    elif not query:
        raise SearchValidationError("搜索内容不能为空")

    return SearchRequest(request.mode, query, request.page)


def _build_search_client(option_file: Path):
    install_safe_jmcomic_logging()
    with _CLIENT_BUILD_LOCK:
        try:
            if (
                jmcomic.JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN
                is not True
            ):
                raise SearchUnavailable()
            if jmcomic.JmModuleConfig.DOMAIN_API_UPDATED_LIST == []:
                jmcomic.JmModuleConfig.DOMAIN_API_UPDATED_LIST = None

            option = jmcomic.create_option_by_file(str(option_file))
            option.client.retry_times = 0
            client = option.new_jm_client(
                impl="api",
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
            if not isinstance(client, jmcomic.JmApiClient):
                raise TypeError("unexpected client type")

            domains = tuple(
                domain.strip()
                for domain in jmcomic.JmModuleConfig.DOMAIN_API_UPDATED_LIST or ()
                if isinstance(domain, str) and domain.strip()
            )
            if not domains:
                raise ValueError("empty domain list")
            if client.get_meta_data("timeout") != SEARCH_TIMEOUT_SECONDS:
                raise ValueError("timeout was not applied")

            client.set_domain_list([domains[0]])
            client.retry_times = SEARCH_REQUEST_RETRIES
            return client
        except SearchError as error:
            LOGGER.warning(
                "Search client creation failed: category=%s",
                error.code,
            )
            raise error from None
        except Exception as error:
            LOGGER.warning(
                "Search client creation failed: category=%s error_type=%s",
                SearchUnavailable.code,
                type(error).__name__,
            )
            raise SearchUnavailable() from None


def _invalidate_api_domain_cache() -> None:
    with _CLIENT_BUILD_LOCK:
        jmcomic.JmModuleConfig.DOMAIN_API_UPDATED_LIST = None


class SearchService:
    def __init__(
        self,
        paths: AppPaths = DEFAULT_PATHS,
        client_factory: Callable[[], object] | None = None,
        cover_url_factory: Callable[[str], str] | None = None,
        max_cover_bytes: int = MAX_COVER_BYTES,
    ):
        if client_factory is not None and not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if cover_url_factory is not None and not callable(cover_url_factory):
            raise TypeError("cover_url_factory must be callable")
        if type(max_cover_bytes) is not int or max_cover_bytes < 1:
            raise ValueError("max_cover_bytes must be a positive integer")

        install_safe_jmcomic_logging()
        self.paths = paths
        self.max_cover_bytes = max_cover_bytes
        self._uses_default_client_factory = client_factory is None
        self._client_factory = client_factory or (
            lambda: _build_search_client(self.paths.option_file)
        )
        self._cover_url_factory = cover_url_factory or (
            lambda album_id: jmcomic.JmcomicText.get_album_cover_url(
                album_id,
                size="_3x4",
            )
        )
        self._thread_state = threading.local()

    def search(self, request: SearchRequest) -> SearchPageSnapshot:
        normalized = normalize_search_request(request)
        client = self._get_client_for_operation("search")

        if normalized.mode is SearchMode.EXACT_ID:
            try:
                album = client.get_album_detail(normalized.query)
            except SearchError as error:
                error = _safe_error_copy(error)
                self._discard_client_for_error(error)
                raise error from None
            except Exception as error:
                self._discard_client_for_error(_map_backend_error(error))
                self._raise_backend_error(error, "search")

            try:
                item = _snapshot_album(album, expected_id=normalized.query)
                return SearchPageSnapshot(normalized, 1, 1, (item,))
            except SearchError:
                raise
            except Exception as error:
                self._raise_response_error(error, "search")

        try:
            method_name = {
                SearchMode.GENERAL: "search_site",
                SearchMode.AUTHOR: "search_author",
                SearchMode.TAG: "search_tag",
            }[normalized.mode]
            page = getattr(client, method_name)(normalized.query, normalized.page)
        except SearchError as error:
            error = _safe_error_copy(error)
            self._discard_client_for_error(error)
            raise error from None
        except Exception as error:
            self._discard_client_for_error(_map_backend_error(error))
            self._raise_backend_error(error, "search")

        try:
            return _snapshot_page(normalized, page)
        except SearchError:
            raise
        except Exception as error:
            self._raise_response_error(error, "search")

    def fetch_cover(self, album_id: str) -> bytes:
        try:
            normalized_id = _normalize_search_album_id(album_id)
        except InvalidAlbumId as error:
            raise SearchValidationError(str(error)) from None

        try:
            cover_url = self._cover_url_factory(normalized_id)
            if not isinstance(cover_url, str) or not cover_url:
                raise SearchResponseError("封面地址无效")
        except SearchError:
            raise
        except Exception as error:
            self._raise_response_error(error, "cover_url")

        client = self._get_client_for_operation("cover")
        try:
            response = client.get_jm_image(cover_url)
        except SearchError as error:
            error = _safe_error_copy(error)
            self._discard_client_for_error(error)
            raise error from None
        except Exception as error:
            self._discard_client_for_error(_map_backend_error(error))
            self._raise_backend_error(error, "cover")

        try:
            http_code = getattr(response, "http_code", None)
            if http_code is not None and (
                type(http_code) is not int or http_code != 200
            ):
                raise SearchResponseError("封面响应无效")

            content = getattr(response, "content", None)
            if not isinstance(content, (bytes, bytearray, memoryview)):
                raise SearchResponseError("封面响应无效")
            content = bytes(content)
            if not content:
                raise SearchResponseError("封面响应为空")
            if len(content) > self.max_cover_bytes:
                raise SearchResponseError("封面响应过大")
            return content
        except SearchError:
            raise
        except Exception as error:
            self._raise_response_error(error, "cover")

    def _get_client_for_operation(self, operation: str):
        client = getattr(self._thread_state, "client", None)
        if client is not None:
            return client

        try:
            install_safe_jmcomic_logging()
            client = self._client_factory()
        except SearchError as error:
            LOGGER.warning(
                "Search client creation failed: category=%s",
                error.code,
            )
            raise _safe_error_copy(error) from None
        except Exception as error:
            self._raise_backend_error(error, operation)

        if client is None:
            self._raise_response_error(TypeError("empty client"), operation)
        self._thread_state.client = client
        return client

    def _discard_thread_client(self) -> None:
        if hasattr(self._thread_state, "client"):
            del self._thread_state.client

    def _discard_client_for_error(self, error: SearchError) -> None:
        if (
            not isinstance(error, SearchUnavailable)
            and type(error) is not SearchError
        ):
            return
        self._discard_thread_client()
        if self._uses_default_client_factory:
            _invalidate_api_domain_cache()

    @staticmethod
    def _raise_backend_error(error: Exception, operation: str):
        mapped = _map_backend_error(error)
        LOGGER.warning(
            "Search backend operation failed: operation=%s category=%s error_type=%s",
            operation,
            mapped.code,
            type(error).__name__,
        )
        raise mapped from None

    @staticmethod
    def _raise_response_error(error: Exception, operation: str):
        LOGGER.warning(
            "Search response could not be adapted: operation=%s error_type=%s",
            operation,
            type(error).__name__,
        )
        raise SearchResponseError() from None


def _snapshot_page(
    request: SearchRequest,
    page,
) -> SearchPageSnapshot:
    if getattr(page, "is_single_album", False) is True:
        album = getattr(page, "single_album", None)
        item = _snapshot_album(album)
        total = _nonnegative_int(getattr(page, "total", None))
        page_count = _nonnegative_int(getattr(page, "page_count", None))
        _validate_page_totals(total, page_count, (item,), request.page)
        return SearchPageSnapshot(request, total, page_count, (item,))

    content = getattr(page, "content", None)
    if content is None or isinstance(content, (str, bytes, bytearray, Mapping)):
        raise SearchResponseError()
    try:
        raw_items = list(content)
    except (TypeError, ValueError):
        raise SearchResponseError() from None

    items = []
    invalid_count = 0
    for raw_item in raw_items:
        item = _snapshot_regular_item(raw_item)
        if item is None:
            invalid_count += 1
            continue
        items.append(item)

    if raw_items and not items:
        raise SearchResponseError()
    if invalid_count:
        LOGGER.warning(
            "Search response skipped invalid items: count=%s",
            invalid_count,
        )

    items_tuple = tuple(items)
    total = _nonnegative_int(getattr(page, "total", None))
    page_count = _nonnegative_int(getattr(page, "page_count", None))
    _validate_page_totals(total, page_count, items_tuple, request.page)
    return SearchPageSnapshot(request, total, page_count, items_tuple)


def _snapshot_regular_item(raw_item) -> SearchResultSnapshot | None:
    if not isinstance(raw_item, (tuple, list)) or len(raw_item) != 2:
        return None

    raw_album_id, raw_info = raw_item
    try:
        album_id = _normalize_search_album_id(raw_album_id)
    except InvalidAlbumId:
        return None

    info = raw_info if isinstance(raw_info, Mapping) else {}
    title = _safe_text(info.get("name")) or _safe_text(info.get("title"))
    authors = _safe_text_tuple(info.get("author"))
    if not authors:
        authors = _safe_text_tuple(info.get("authors"))
    tags = _safe_text_tuple(info.get("tags"))
    return SearchResultSnapshot(album_id, title, authors, tags)


def _snapshot_album(album, expected_id: str | None = None) -> SearchResultSnapshot:
    if album is None:
        raise SearchResponseError()

    try:
        album_id = _normalize_search_album_id(
            getattr(album, "album_id", None)
        )
    except InvalidAlbumId:
        raise SearchResponseError() from None
    if expected_id is not None and album_id != expected_id:
        raise SearchResponseError()

    title = _safe_text(getattr(album, "title", None)) or _safe_text(
        getattr(album, "name", None)
    )
    authors = _safe_text_tuple(getattr(album, "authors", None))
    tags = _safe_text_tuple(getattr(album, "tags", None))
    return SearchResultSnapshot(album_id, title, authors, tags)


def _safe_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _normalize_search_album_id(value) -> str:
    album_id = normalize_album_id(value)
    return str(int(album_id))


def _safe_text_tuple(value) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return ()

    result = []
    seen = set()
    for raw_value in values:
        normalized = _safe_text(raw_value)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _nonnegative_int(value) -> int:
    if type(value) is int:
        result = value
    elif (
        isinstance(value, str)
        and value.strip().isascii()
        and value.strip().isdigit()
    ):
        result = int(value.strip())
    else:
        raise SearchResponseError()
    if result < 0:
        raise SearchResponseError()
    return result


def _validate_page_totals(
    total: int,
    page_count: int,
    items: tuple[SearchResultSnapshot, ...],
    requested_page: int,
) -> None:
    if items and (total == 0 or page_count == 0):
        raise SearchResponseError()
    if (total == 0) != (page_count == 0):
        raise SearchResponseError()
    if total < len(items):
        raise SearchResponseError()
    if page_count and requested_page > page_count:
        raise SearchResponseError()


def _map_backend_error(error: Exception) -> SearchError:
    if isinstance(error, SearchError):
        return error
    if isinstance(error, jmcomic.MissingAlbumPhotoException):
        return SearchNotFound()
    if isinstance(
        error,
        (
            jmcomic.RequestRetryAllFailException,
            RequestException,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return SearchUnavailable()
    if isinstance(error, jmcomic.ResponseUnexpectedException):
        status = _response_status(error)
        if status is not None and 400 <= status < 500:
            return SearchRejected()
        return SearchResponseError()
    if isinstance(
        error,
        (
            jmcomic.RegularNotMatchException,
            jmcomic.JsonResolveFailException,
            jmcomic.JmcomicException,
        ),
    ):
        return SearchResponseError()
    return SearchError()


def _safe_error_copy(error: SearchError) -> SearchError:
    for error_type in (
        SearchValidationError,
        SearchRejected,
        SearchNotFound,
        SearchUnavailable,
        SearchResponseError,
    ):
        if isinstance(error, error_type):
            return error_type()
    return SearchError()


def _response_status(error: Exception) -> int | None:
    try:
        response = error.resp
    except Exception:
        return None
    for attribute in ("http_code", "status_code"):
        try:
            value = getattr(response, attribute)
        except Exception:
            continue
        if type(value) is int:
            return value
    return None


__all__ = [
    "MAX_COVER_BYTES",
    "SEARCH_REQUEST_RETRIES",
    "SEARCH_TIMEOUT_SECONDS",
    "SearchError",
    "SearchNotFound",
    "SearchRejected",
    "SearchResponseError",
    "SearchService",
    "SearchUnavailable",
    "SearchValidationError",
    "normalize_search_request",
]
