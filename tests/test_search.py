from dataclasses import FrozenInstanceError
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

import jmcomic

from jm_downloader import jmcomic_logging, search
from jm_downloader.models import (
    SearchMode,
    SearchPageSnapshot,
    SearchRequest,
    SearchResultSnapshot,
)
from jm_downloader.search import (
    SearchError,
    SearchNotFound,
    SearchRejected,
    SearchResponseError,
    SearchService,
    SearchUnavailable,
    SearchValidationError,
    normalize_search_request,
)
from jm_downloader.settings import AppPaths


_UNSET = object()


class FakePage:
    def __init__(
        self,
        content,
        total=None,
        page_count=None,
        single_album=_UNSET,
    ):
        self.content = content
        self.total = len(content) if total is None else total
        if page_count is None:
            page_count = 1 if self.total else 0
        self.page_count = page_count
        self.is_single_album = single_album is not _UNSET
        if self.is_single_album:
            self.single_album = single_album


class FakeSearchClient:
    def __init__(
        self,
        page=None,
        album=None,
        image_response=None,
        errors=None,
    ):
        self.page = page or FakePage([], total=0, page_count=0)
        self.album = album
        self.image_response = image_response
        self.errors = errors or {}
        self.calls = []

    def _call(self, method, *args):
        self.calls.append((method, *args))
        error = self.errors.get(method)
        if error is not None:
            raise error

    def search_site(self, query, page):
        self._call("search_site", query, page)
        return self.page

    def search_author(self, query, page):
        self._call("search_author", query, page)
        return self.page

    def search_tag(self, query, page):
        self._call("search_tag", query, page)
        return self.page

    def get_album_detail(self, album_id):
        self._call("get_album_detail", album_id)
        return self.album

    def get_jm_image(self, url):
        self._call("get_jm_image", url)
        return self.image_response


def make_album(
    album_id="1449491",
    name="测试漫画",
    authors=None,
    tags=None,
    title=None,
):
    return SimpleNamespace(
        album_id=album_id,
        name=name,
        title=title,
        authors=[] if authors is None else authors,
        tags=[] if tags is None else tags,
    )


class SearchModelTests(unittest.TestCase):
    def test_search_models_are_frozen_slotted_value_objects(self):
        request = SearchRequest(SearchMode.GENERAL, "keyword", 2)
        item = SearchResultSnapshot(
            "123456",
            "标题",
            ("作者",),
            ("标签",),
        )
        page = SearchPageSnapshot(request, 81, 2, (item,))

        for value, attribute, replacement in (
            (request, "query", "changed"),
            (item, "title", "changed"),
            (page, "total", 0),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, attribute, replacement)
                self.assertFalse(hasattr(value, "__dict__"))

        self.assertIsInstance(page.items, tuple)
        self.assertIsInstance(item.authors, tuple)
        self.assertIsInstance(item.tags, tuple)
        self.assertEqual(
            [mode.value for mode in SearchMode],
            ["general", "author", "tag", "exact_id"],
        )

    def test_service_copies_mutable_upstream_values_into_immutable_models(self):
        authors = [" Alice ", "Bob"]
        tags = ["tag-a", "tag-b"]
        info = {
            "name": "  A\n title  ",
            "author": authors,
            "tags": tags,
            "image": "https://secret.invalid/cover.jpg",
        }
        page = FakePage([("00123", info)], total=1, page_count=1)
        result = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        ).search(SearchRequest(SearchMode.GENERAL, " query "))

        authors.append("Late author")
        tags.clear()
        info["name"] = "Late title"

        self.assertEqual(result.request.query, "query")
        self.assertEqual(result.items[0].album_id, "123")
        self.assertEqual(result.items[0].title, "A title")
        self.assertEqual(result.items[0].authors, ("Alice", "Bob"))
        self.assertEqual(result.items[0].tags, ("tag-a", "tag-b"))
        for forbidden in ("image", "cover", "cover_url", "url", "upstream"):
            self.assertFalse(hasattr(result.items[0], forbidden))


class SearchValidationTests(unittest.TestCase):
    def test_normalizes_queries_and_exact_album_ids(self):
        cases = (
            (
                SearchRequest(SearchMode.GENERAL, "  a  ", 3),
                SearchRequest(SearchMode.GENERAL, "a", 3),
            ),
            (
                SearchRequest(SearchMode.EXACT_ID, " jm00123 "),
                SearchRequest(SearchMode.EXACT_ID, "123", 1),
            ),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(normalize_search_request(original), expected)

    def test_rejects_invalid_requests_before_creating_a_client(self):
        factory_calls = []
        service = SearchService(
            client_factory=lambda: factory_calls.append(True)
        )
        invalid_requests = (
            object(),
            SearchRequest("general", "query"),
            SearchRequest(SearchMode.GENERAL, ""),
            SearchRequest(SearchMode.GENERAL, " \t "),
            SearchRequest(SearchMode.GENERAL, 123),
            SearchRequest(SearchMode.GENERAL, "query", 0),
            SearchRequest(SearchMode.GENERAL, "query", -1),
            SearchRequest(SearchMode.GENERAL, "query", True),
            SearchRequest(SearchMode.GENERAL, "query", 1.5),
            SearchRequest(SearchMode.GENERAL, "query", "1"),
            SearchRequest(SearchMode.EXACT_ID, "JM"),
            SearchRequest(SearchMode.EXACT_ID, "１２３"),
            SearchRequest(SearchMode.EXACT_ID, "12/3"),
            SearchRequest(SearchMode.EXACT_ID, "JMJM123"),
            SearchRequest(SearchMode.EXACT_ID, "123", 2),
        )

        for request in invalid_requests:
            with self.subTest(request=request):
                with self.assertRaises(SearchValidationError):
                    service.search(request)

        self.assertEqual(factory_calls, [])

    def test_rejects_invalid_service_dependencies_and_cover_limit(self):
        for kwargs in (
            {"client_factory": object()},
            {"cover_url_factory": object()},
            {"max_cover_bytes": 0},
            {"max_cover_bytes": -1},
            {"max_cover_bytes": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    SearchService(**kwargs)


class SearchRoutingTests(unittest.TestCase):
    def test_routes_three_page_modes_to_the_matching_client_method(self):
        expected_methods = {
            SearchMode.GENERAL: "search_site",
            SearchMode.AUTHOR: "search_author",
            SearchMode.TAG: "search_tag",
        }
        for mode, method in expected_methods.items():
            with self.subTest(mode=mode):
                client = FakeSearchClient(
                    page=FakePage([], total=0, page_count=0)
                )
                result = SearchService(client_factory=lambda: client).search(
                    SearchRequest(mode, "  keyword  ", 3)
                )

                self.assertEqual(client.calls, [(method, "keyword", 3)])
                self.assertEqual(result.request, SearchRequest(mode, "keyword", 3))
                self.assertEqual(result.items, ())

    def test_regular_search_does_not_fetch_details_for_each_result(self):
        content = [
            (str(album_id), {"name": f"Title {album_id}", "author": "A"})
            for album_id in range(1, 81)
        ]
        client = FakeSearchClient(
            page=FakePage(content, total=160, page_count=2),
            errors={"get_album_detail": AssertionError("N+1 detail fetch")},
        )

        result = SearchService(client_factory=lambda: client).search(
            SearchRequest(SearchMode.GENERAL, "keyword")
        )

        self.assertEqual(len(result.items), 80)
        self.assertEqual(client.calls, [("search_site", "keyword", 1)])

    def test_exact_search_fetches_one_detail_without_starting_download(self):
        client = FakeSearchClient(
            album=make_album(
                authors=["Author"],
                tags=["tag"],
            )
        )
        with patch.object(
            search.jmcomic,
            "download_album",
            side_effect=AssertionError("download must not start"),
        ) as download_album:
            result = SearchService(client_factory=lambda: client).search(
                SearchRequest(SearchMode.EXACT_ID, "JM1449491")
            )

        self.assertEqual(client.calls, [("get_album_detail", "1449491")])
        self.assertEqual(result.total, 1)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.items[0].authors, ("Author",))
        self.assertEqual(result.items[0].tags, ("tag",))
        download_album.assert_not_called()

    def test_exact_search_canonicalizes_leading_zero_album_ids(self):
        client = FakeSearchClient(album=make_album(album_id="123"))

        result = SearchService(client_factory=lambda: client).search(
            SearchRequest(SearchMode.EXACT_ID, "JM00123")
        )

        self.assertEqual(client.calls, [("get_album_detail", "123")])
        self.assertEqual(result.request.query, "123")
        self.assertEqual(result.items[0].album_id, "123")


class SearchAdaptationTests(unittest.TestCase):
    def test_empty_page_is_a_successful_result(self):
        page = FakePage([], total=0, page_count=0)

        result = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        ).search(SearchRequest(SearchMode.GENERAL, "missing"))

        self.assertEqual(result.total, 0)
        self.assertEqual(result.page_count, 0)
        self.assertEqual(result.items, ())

    def test_normalizes_optional_fields_without_inventing_metadata(self):
        source = [
            (
                "1",
                {
                    "name": None,
                    "author": [
                        " Alice ",
                        "",
                        "Alice",
                        None,
                        7,
                        {"bad": "value"},
                        "Bob\nSmith",
                    ],
                    "tags": (
                        " tag ",
                        "tag",
                        "",
                        None,
                        object(),
                        "second",
                    ),
                },
            ),
            ("2", {"title": " fallback ", "authors": ["Author"]}),
            ("3", ["not", "a", "mapping"]),
        ]
        page = FakePage(source, total=3, page_count=1)

        result = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        ).search(SearchRequest(SearchMode.TAG, "query"))

        self.assertEqual(result.items[0].title, None)
        self.assertEqual(result.items[0].authors, ("Alice", "Bob Smith"))
        self.assertEqual(result.items[0].tags, ("tag", "second"))
        self.assertEqual(result.items[1].title, "fallback")
        self.assertEqual(result.items[1].authors, ("Author",))
        self.assertEqual(result.items[1].tags, ())
        self.assertEqual(result.items[2].title, None)
        self.assertEqual(result.items[2].authors, ())
        self.assertEqual(result.items[2].tags, ())
        self.assertEqual(source[0][1]["tags"][0], " tag ")

    def test_skips_bad_items_but_preserves_good_item_order(self):
        page = FakePage(
            [
                None,
                ("bad/id", {"name": "bad"}),
                ("2", {"name": "second"}),
                ("1", {"name": "first"}),
                ("too", "many", "parts"),
            ],
            total=2,
            page_count=1,
        )

        result = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        ).search(SearchRequest(SearchMode.GENERAL, "query"))

        self.assertEqual(
            [(item.album_id, item.title) for item in result.items],
            [("2", "second"), ("1", "first")],
        )

    def test_rejects_pages_without_any_valid_items_or_valid_totals(self):
        pages = (
            FakePage([None, ("bad", {})], total=2, page_count=1),
            SimpleNamespace(content={"1": {}}, total=1, page_count=1),
            SimpleNamespace(content=[]),
            FakePage([("1", {})], total=0, page_count=0),
            FakePage([], total=0, page_count=1),
            FakePage([], total=1, page_count=0),
            FakePage(
                [("1", {}), ("2", {})],
                total=1,
                page_count=1,
            ),
            FakePage([], total=-1, page_count=0),
            FakePage([], total=True, page_count=0),
            FakePage([], total="many", page_count=0),
        )
        for page in pages:
            with self.subTest(page=page):
                service = SearchService(
                    client_factory=lambda page=page: FakeSearchClient(page=page)
                )
                with self.assertRaises(SearchResponseError):
                    service.search(SearchRequest(SearchMode.GENERAL, "query"))

    def test_accepts_numeric_string_totals(self):
        page = FakePage(
            [("1", {"name": "one"})],
            total="81",
            page_count="2",
        )

        result = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        ).search(SearchRequest(SearchMode.GENERAL, "query", 2))

        self.assertEqual(result.total, 81)
        self.assertEqual(result.page_count, 2)

    def test_rejects_requested_page_beyond_reported_page_count(self):
        page = FakePage([], total=81, page_count=2)
        service = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        )

        with self.assertRaises(SearchResponseError):
            service.search(SearchRequest(SearchMode.GENERAL, "query", 3))

    def test_single_album_redirect_uses_detail_metadata(self):
        album = make_album(
            album_id="456",
            name=" Detail name ",
            authors=["Author", "Author", "Second"],
            tags=["tag", "", "tag"],
        )
        page = FakePage(
            [("456", {"name": "Search title", "author": "Wrong"})],
            total=1,
            page_count=1,
            single_album=album,
        )

        result = SearchService(
            client_factory=lambda: FakeSearchClient(page=page)
        ).search(SearchRequest(SearchMode.GENERAL, "456"))

        self.assertEqual(result.items[0].title, "Detail name")
        self.assertEqual(result.items[0].authors, ("Author", "Second"))
        self.assertEqual(result.items[0].tags, ("tag",))

    def test_exact_detail_rejects_missing_invalid_or_mismatched_id(self):
        albums = (
            None,
            SimpleNamespace(name="missing id", authors=[], tags=[]),
            make_album(album_id="bad/id"),
            make_album(album_id="999"),
        )
        for album in albums:
            with self.subTest(album=album):
                service = SearchService(
                    client_factory=lambda album=album: FakeSearchClient(
                        album=album
                    )
                )
                with self.assertRaises(SearchResponseError):
                    service.search(
                        SearchRequest(SearchMode.EXACT_ID, "1449491")
                    )

    def test_exact_detail_keeps_missing_optional_metadata_empty(self):
        album = make_album(
            album_id="1449491",
            name=" \t ",
            authors=None,
            tags=None,
        )

        result = SearchService(
            client_factory=lambda: FakeSearchClient(album=album)
        ).search(SearchRequest(SearchMode.EXACT_ID, "1449491"))

        self.assertIsNone(result.items[0].title)
        self.assertEqual(result.items[0].authors, ())
        self.assertEqual(result.items[0].tags, ())


class SearchErrorTests(unittest.TestCase):
    def test_maps_backend_failures_to_stable_sanitized_categories(self):
        secret = "VERY_SECRET_QUERY_TOKEN"
        cases = (
            (
                jmcomic.MissingAlbumPhotoException(
                    secret,
                    {"missing_jm_id": "1"},
                ),
                SearchNotFound,
                "not_found",
            ),
            (
                jmcomic.RequestRetryAllFailException(secret, {}),
                SearchUnavailable,
                "unavailable",
            ),
            (TimeoutError(secret), SearchUnavailable, "unavailable"),
            (
                jmcomic.ResponseUnexpectedException(
                    secret,
                    {"resp": SimpleNamespace(status_code=403)},
                ),
                SearchRejected,
                "rejected",
            ),
            (
                jmcomic.ResponseUnexpectedException(
                    secret,
                    {"resp": SimpleNamespace(status_code=500)},
                ),
                SearchResponseError,
                "invalid_response",
            ),
            (
                jmcomic.RegularNotMatchException(secret, {}),
                SearchResponseError,
                "invalid_response",
            ),
            (RuntimeError(secret), SearchError, "unknown"),
        )

        for backend_error, expected_type, expected_code in cases:
            with self.subTest(error=type(backend_error).__name__):
                client = FakeSearchClient(
                    errors={"search_site": backend_error}
                )
                service = SearchService(client_factory=lambda: client)
                with self.assertLogs("jm-downloader", logging.WARNING) as logs:
                    with self.assertRaises(expected_type) as raised:
                        service.search(
                            SearchRequest(SearchMode.GENERAL, secret)
                        )

                self.assertEqual(raised.exception.code, expected_code)
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn(secret, "\n".join(logs.output))

    def test_maps_client_factory_failure_and_discards_failed_client(self):
        clients = [
            FakeSearchClient(errors={"search_site": TimeoutError("secret")}),
            FakeSearchClient(page=FakePage([], total=0, page_count=0)),
        ]
        calls = []

        def factory():
            calls.append(True)
            return clients[len(calls) - 1]

        service = SearchService(client_factory=factory)
        with self.assertRaises(SearchUnavailable):
            service.search(SearchRequest(SearchMode.GENERAL, "first"))
        result = service.search(SearchRequest(SearchMode.GENERAL, "second"))

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.items, ())

        with self.assertRaises(SearchUnavailable):
            SearchService(
                client_factory=lambda: (_ for _ in ()).throw(
                    TimeoutError("secret")
                )
            ).search(SearchRequest(SearchMode.GENERAL, "query"))

    def test_does_not_swallow_process_control_exceptions(self):
        client = FakeSearchClient(
            errors={"search_site": KeyboardInterrupt()}
        )
        with self.assertRaises(KeyboardInterrupt):
            SearchService(client_factory=lambda: client).search(
                SearchRequest(SearchMode.GENERAL, "query")
            )

    def test_public_traceback_suppresses_backend_secret(self):
        secret = "SECRET_IN_BACKEND_EXCEPTION"
        client = FakeSearchClient(
            errors={"search_site": RuntimeError(secret)}
        )

        with self.assertRaises(SearchError) as raised:
            SearchService(client_factory=lambda: client).search(
                SearchRequest(SearchMode.GENERAL, "query")
            )

        formatted = "".join(
            traceback.format_exception(raised.exception)
        )
        self.assertNotIn(secret, formatted)
        self.assertNotIn("RuntimeError", formatted)

    def test_not_found_keeps_healthy_thread_client(self):
        missing = jmcomic.MissingAlbumPhotoException(
            "secret",
            {"missing_jm_id": "1"},
        )
        client = FakeSearchClient(errors={"get_album_detail": missing})
        factory_calls = []
        service = SearchService(
            client_factory=lambda: factory_calls.append(True) or client
        )

        with self.assertRaises(SearchNotFound):
            service.search(SearchRequest(SearchMode.EXACT_ID, "1"))
        client.errors.clear()
        client.page = FakePage([], total=0, page_count=0)
        service.search(SearchRequest(SearchMode.GENERAL, "query"))

        self.assertEqual(factory_calls, [True])


class SafeJmcomicLoggingTests(unittest.TestCase):
    def setUp(self):
        config = jmcomic.JmModuleConfig
        original_executor = config.EXECUTOR_LOG
        original_dump = config.FLAG_DUMP_HTML_ON_REGEX_ERROR
        original_enabled = config.FLAG_ENABLE_JM_LOG
        self.addCleanup(setattr, config, "EXECUTOR_LOG", original_executor)
        self.addCleanup(
            setattr,
            config,
            "FLAG_DUMP_HTML_ON_REGEX_ERROR",
            original_dump,
        )
        self.addCleanup(
            setattr,
            config,
            "FLAG_ENABLE_JM_LOG",
            original_enabled,
        )
        config.FLAG_ENABLE_JM_LOG = True

    def test_drops_upstream_messages_urls_credentials_and_tracebacks(self):
        secret = "ULTRA_SECRET_SENTINEL"
        jmcomic_logging.install_safe_jmcomic_logging()

        with self.assertLogs("jm-downloader", logging.DEBUG) as logs:
            jmcomic.JmModuleConfig.jm_log(
                "req.retry",
                f"https://example.invalid/search?q={secret} "
                f"headers=Cookie token={secret}",
            )
            jmcomic.JmModuleConfig.jm_log(
                "req.error",
                f"raw message {secret}",
                RuntimeError(secret),
            )
            jmcomic.JmModuleConfig.jm_log(
                f"req.retry?token={secret}",
                secret,
            )

        output = "\n".join(logs.output)
        self.assertIn("JM request retrying", output)
        self.assertIn("JM request attempt failed (RuntimeError)", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("https://", output)
        self.assertNotIn("Cookie", output)
        self.assertNotIn("Traceback", output)
        self.assertFalse(
            jmcomic.JmModuleConfig.FLAG_DUMP_HTML_ON_REGEX_ERROR
        )

    def test_installer_is_idempotent_and_thread_safe(self):
        jmcomic.JmModuleConfig.EXECUTOR_LOG = lambda *args: None
        threads = [
            threading.Thread(
                target=jmcomic_logging.install_safe_jmcomic_logging
            )
            for _ in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertIs(
            jmcomic.JmModuleConfig.EXECUTOR_LOG,
            jmcomic_logging._safe_jmcomic_log,
        )
        with self.assertLogs("jm-downloader", logging.WARNING) as logs:
            jmcomic.JmModuleConfig.jm_log("req.retry", "private")
        self.assertEqual(len(logs.records), 1)
        self.assertTrue(jmcomic.JmModuleConfig.FLAG_ENABLE_JM_LOG)


class DefaultSearchClientFactoryTests(unittest.TestCase):
    class FakeApiClient:
        def __init__(self, domains):
            self.domains = list(domains)
            self.retry_times = None
            self.timeout = None

        def get_domain_list(self):
            return list(self.domains)

        def set_domain_list(self, domains):
            self.domains = list(domains)

        def get_meta_data(self, name):
            if name == "timeout":
                return self.timeout
            return None

    class FakeOption:
        def __init__(self, client, discovered_domains=_UNSET):
            self.client = SimpleNamespace(retry_times=5)
            self.created_client = client
            self.discovered_domains = discovered_domains
            self.calls = []

        def new_jm_client(self, **kwargs):
            self.calls.append((self.client.retry_times, kwargs))
            self.created_client.timeout = kwargs.get("timeout")
            if self.discovered_domains is not _UNSET:
                jmcomic.JmModuleConfig.DOMAIN_API_UPDATED_LIST = list(
                    self.discovered_domains
                )
            return self.created_client

    def test_uses_portable_option_and_applies_bounded_config_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            paths.option_file.write_bytes(b"original option bytes")
            before = paths.option_file.read_bytes()
            client = self.FakeApiClient(
                [" ", "api-one.invalid", "api-two.invalid"]
            )
            option = self.FakeOption(
                client,
                ["api-one.invalid", "api-two.invalid"],
            )
            install_seen = []

            def create_option(option_path):
                self.assertEqual(option_path, str(paths.option_file))
                install_seen.append(
                    jmcomic.JmModuleConfig.EXECUTOR_LOG
                    is jmcomic_logging._safe_jmcomic_log
                )
                return option

            with (
                patch.object(
                    search.jmcomic,
                    "create_option_by_file",
                    side_effect=create_option,
                ),
                patch.object(
                    search.jmcomic,
                    "JmApiClient",
                    self.FakeApiClient,
                ),
                patch.object(
                    search.jmcomic.JmModuleConfig,
                    "DOMAIN_API_UPDATED_LIST",
                    None,
                ),
            ):
                result = search._build_search_client(paths.option_file)

            self.assertIs(result, client)
            self.assertEqual(install_seen, [True])
            self.assertEqual(
                option.calls,
                [
                    (
                        0,
                        {
                            "impl": "api",
                            "timeout": search.SEARCH_TIMEOUT_SECONDS,
                        },
                    )
                ],
            )
            self.assertEqual(client.domains, ["api-one.invalid"])
            self.assertEqual(
                client.retry_times,
                search.SEARCH_REQUEST_RETRIES,
            )
            self.assertEqual(paths.option_file.read_bytes(), before)
            self.assertFalse(paths.pictures.exists())
            self.assertFalse(paths.pdfs.exists())
            self.assertFalse(paths.logs.exists())
            self.assertEqual(
                [path.name for path in paths.root.iterdir()],
                ["option.yml"],
            )

    def test_rejects_empty_discovery_even_when_client_keeps_stale_domain(self):
        client = self.FakeApiClient(["stale-builtin.invalid"])
        option = self.FakeOption(client, [])
        with (
            patch.object(
                search.jmcomic,
                "create_option_by_file",
                return_value=option,
            ),
            patch.object(
                search.jmcomic,
                "JmApiClient",
                self.FakeApiClient,
            ),
            patch.object(
                search.jmcomic.JmModuleConfig,
                "DOMAIN_API_UPDATED_LIST",
                None,
            ),
        ):
            with self.assertRaises(SearchUnavailable) as raised:
                search._build_search_client(Path("missing-option.yml"))

        self.assertEqual(raised.exception.code, "unavailable")
        self.assertNotIn("domain", str(raised.exception).lower())

    def test_retries_domain_discovery_after_cached_empty_result(self):
        config = jmcomic.JmModuleConfig
        original_updated = config.DOMAIN_API_UPDATED_LIST
        original_auto_update = config.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN
        self.addCleanup(
            setattr,
            config,
            "DOMAIN_API_UPDATED_LIST",
            original_updated,
        )
        self.addCleanup(
            setattr,
            config,
            "FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN",
            original_auto_update,
        )
        config.DOMAIN_API_UPDATED_LIST = []
        config.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = True

        client = self.FakeApiClient(["api-current.invalid"])
        option = self.FakeOption(client, ["api-current.invalid"])

        def create_option(_path):
            self.assertIsNone(config.DOMAIN_API_UPDATED_LIST)
            return option

        with (
            patch.object(
                search.jmcomic,
                "create_option_by_file",
                side_effect=create_option,
            ),
            patch.object(
                search.jmcomic,
                "JmApiClient",
                self.FakeApiClient,
            ),
        ):
            self.assertIs(
                search._build_search_client(Path("option.yml")),
                client,
            )

    def test_requires_automatic_domain_discovery(self):
        config = jmcomic.JmModuleConfig
        original = config.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN
        self.addCleanup(
            setattr,
            config,
            "FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN",
            original,
        )
        config.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False

        with patch.object(
            search.jmcomic,
            "create_option_by_file",
        ) as create_option:
            with self.assertRaises(SearchUnavailable):
                search._build_search_client(Path("option.yml"))

        create_option.assert_not_called()

    def test_network_failure_invalidates_default_domain_cache(self):
        config = jmcomic.JmModuleConfig
        original = config.DOMAIN_API_UPDATED_LIST
        self.addCleanup(
            setattr,
            config,
            "DOMAIN_API_UPDATED_LIST",
            original,
        )
        config.DOMAIN_API_UPDATED_LIST = ["stale.invalid"]

        clients = [
            FakeSearchClient(
                errors={"search_site": TimeoutError("secret")}
            ),
            FakeSearchClient(page=FakePage([], total=0, page_count=0)),
        ]
        factory_calls = []
        service = SearchService()
        service._client_factory = lambda: (
            factory_calls.append(True) or clients[len(factory_calls) - 1]
        )

        with self.assertRaises(SearchUnavailable):
            service.search(SearchRequest(SearchMode.GENERAL, "first"))
        self.assertIsNone(config.DOMAIN_API_UPDATED_LIST)
        result = service.search(SearchRequest(SearchMode.GENERAL, "second"))

        self.assertEqual(result.items, ())
        self.assertEqual(len(factory_calls), 2)


class SearchThreadLocalClientTests(unittest.TestCase):
    def test_reuses_client_in_one_thread_but_never_across_threads(self):
        clients = []
        factory_lock = threading.Lock()

        def factory():
            with factory_lock:
                client = FakeSearchClient(
                    page=FakePage([], total=0, page_count=0),
                    image_response=SimpleNamespace(content=b"cover"),
                )
                clients.append(client)
                return client

        service = SearchService(
            client_factory=factory,
            cover_url_factory=lambda album_id: f"memory://{album_id}",
        )
        service.search(SearchRequest(SearchMode.GENERAL, "main"))
        self.assertEqual(service.fetch_cover("1"), b"cover")

        results = []
        errors = []

        def search_on_worker():
            try:
                results.append(
                    service.search(
                        SearchRequest(SearchMode.AUTHOR, "worker")
                    )
                )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=search_on_worker)
        worker.start()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(clients), 2)
        self.assertEqual(
            clients[0].calls,
            [
                ("search_site", "main", 1),
                ("get_jm_image", "memory://1"),
            ],
        )
        self.assertEqual(
            clients[1].calls,
            [("search_author", "worker", 1)],
        )


class SearchCoverTests(unittest.TestCase):
    def test_default_cover_url_uses_normalized_id_and_three_by_four_size(self):
        client = FakeSearchClient(
            image_response=SimpleNamespace(content=b"cover")
        )
        with patch.object(
            search.jmcomic.JmcomicText,
            "get_album_cover_url",
            return_value="https://image.invalid/cover.jpg",
        ) as get_cover_url:
            result = SearchService(client_factory=lambda: client).fetch_cover(
                "JM00123"
            )

        self.assertEqual(result, b"cover")
        get_cover_url.assert_called_once_with("123", size="_3x4")
        self.assertEqual(
            client.calls,
            [("get_jm_image", "https://image.invalid/cover.jpg")],
        )

    def test_fetches_cover_bytes_without_exposing_url_or_writing_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = AppPaths(Path(temp_dir))
            url_calls = []
            client = FakeSearchClient(
                image_response=SimpleNamespace(
                    http_code=200,
                    content=bytearray(b"cover bytes"),
                )
            )
            service = SearchService(
                paths=paths,
                client_factory=lambda: client,
                cover_url_factory=lambda album_id: (
                    url_calls.append(album_id) or f"memory://cover/{album_id}"
                ),
            )

            result = service.fetch_cover(" JM00123 ")

            self.assertEqual(result, b"cover bytes")
            self.assertIsInstance(result, bytes)
            self.assertEqual(url_calls, ["123"])
            self.assertEqual(
                client.calls,
                [("get_jm_image", "memory://cover/123")],
            )
            self.assertEqual(list(paths.root.iterdir()), [])

    def test_accepts_exact_limit_and_rejects_invalid_cover_responses(self):
        accepted = (
            b"1234",
            bytearray(b"1234"),
            memoryview(b"1234"),
        )
        for content in accepted:
            with self.subTest(content_type=type(content).__name__):
                client = FakeSearchClient(
                    image_response=SimpleNamespace(content=content)
                )
                result = SearchService(
                    client_factory=lambda client=client: client,
                    cover_url_factory=lambda album_id: "memory://cover",
                    max_cover_bytes=4,
                ).fetch_cover("1")
                self.assertEqual(result, b"1234")

        rejected = (
            SimpleNamespace(content=b""),
            SimpleNamespace(content=None),
            SimpleNamespace(content="not bytes"),
            SimpleNamespace(content=b"12345"),
            SimpleNamespace(http_code=404, content=b"1234"),
            SimpleNamespace(http_code="200", content=b"1234"),
        )
        for response in rejected:
            with self.subTest(response=response):
                client = FakeSearchClient(image_response=response)
                service = SearchService(
                    client_factory=lambda client=client: client,
                    cover_url_factory=lambda album_id: "memory://cover",
                    max_cover_bytes=4,
                )
                with self.assertRaises(SearchResponseError):
                    service.fetch_cover("1")

    def test_cover_validation_and_network_failure_are_stable(self):
        factory_calls = []
        url_calls = []
        service = SearchService(
            client_factory=lambda: factory_calls.append(True),
            cover_url_factory=lambda album_id: url_calls.append(album_id),
        )
        for album_id in ("", "JM", "１２３", "1/2"):
            with self.subTest(album_id=album_id):
                with self.assertRaises(SearchValidationError):
                    service.fetch_cover(album_id)
        self.assertEqual(factory_calls, [])
        self.assertEqual(url_calls, [])

        secret = "SECRET_COVER_URL_OR_TOKEN"
        client = FakeSearchClient(
            errors={"get_jm_image": TimeoutError(secret)}
        )
        service = SearchService(
            client_factory=lambda: client,
            cover_url_factory=lambda album_id: f"https://invalid/{secret}",
        )
        with self.assertLogs("jm-downloader", logging.WARNING) as logs:
            with self.assertRaises(SearchUnavailable) as raised:
                service.fetch_cover("1")

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
