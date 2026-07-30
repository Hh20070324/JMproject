import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jm_downloader.models import (
    ChapterSnapshot,
    SearchMode,
    SearchRequest,
)
from jm_downloader.search import SearchService, SearchUnavailable


def make_page():
    return SimpleNamespace(
        content=(
            (
                "123",
                {
                    "name": "Example",
                    "author": ["Author"],
                    "tags": ["Tag"],
                },
            ),
        ),
        total=1,
        page_count=1,
        is_single_album=False,
    )


def make_album():
    return SimpleNamespace(
        album_id="123",
        title="Example",
        name="Example",
        authors=["Author"],
        tags=["Tag"],
        episode_list=[
            ("301", "1", "First"),
            ("302", "2", "Second"),
        ],
    )


class SyncClient:
    def __init__(self):
        self.calls = []

    def search_site(self, query, page):
        self.calls.append(("search_site", query, page))
        return make_page()

    def search_author(self, query, page):
        self.calls.append(("search_author", query, page))
        return make_page()

    def search_tag(self, query, page):
        self.calls.append(("search_tag", query, page))
        return make_page()

    def get_album_detail(self, album_id):
        self.calls.append(("get_album_detail", album_id))
        return make_album()


class AsyncClient:
    def __init__(self, *, setup_error=None, request_error=None):
        self.setup_error = setup_error
        self.request_error = request_error
        self.setup_called = 0
        self.close_called = 0
        self.calls = []

    async def setup(self):
        self.setup_called += 1
        if self.setup_error is not None:
            raise self.setup_error

    async def close(self):
        self.close_called += 1

    async def _result(self, method, *args):
        self.calls.append((method, *args))
        if self.request_error is not None:
            raise self.request_error
        return make_album() if method == "get_album_detail" else make_page()

    async def search_site(self, query, page):
        return await self._result("search_site", query, page)

    async def search_author(self, query, page):
        return await self._result("search_author", query, page)

    async def search_tag(self, query, page):
        return await self._result("search_tag", query, page)

    async def get_album_detail(self, album_id):
        return await self._result("get_album_detail", album_id)


class V321AsyncSearchTests(unittest.TestCase):
    def test_four_modes_match_sync_snapshots_and_close_each_client(self):
        requests = (
            SearchRequest(SearchMode.GENERAL, "query"),
            SearchRequest(SearchMode.AUTHOR, "query"),
            SearchRequest(SearchMode.TAG, "query"),
            SearchRequest(SearchMode.EXACT_ID, "123"),
        )
        for request in requests:
            with self.subTest(mode=request.mode):
                sync = SearchService(client_factory=SyncClient).search(
                    request,
                    query_engine="sync",
                )
                active = AsyncClient()
                async_result = SearchService(
                    async_client_factory=lambda _route: active,
                ).search(request, query_engine="async")
                self.assertEqual(async_result, sync)
                self.assertEqual(active.setup_called, 1)
                self.assertEqual(active.close_called, 1)

    def test_chapter_catalog_matches_sync_and_closes_client(self):
        sync = SearchService(client_factory=SyncClient).fetch_chapters(
            "123",
            query_engine="sync",
        )
        active = AsyncClient()
        async_result = SearchService(
            async_client_factory=lambda _route: active,
        ).fetch_chapters("123", query_engine="async")

        self.assertEqual(async_result, sync)
        self.assertEqual(
            async_result.chapters,
            (
                ChapterSnapshot("301", 1, "First"),
                ChapterSnapshot("302", 2, "Second"),
            ),
        )
        self.assertEqual(active.close_called, 1)

    def test_fixed_route_is_snapshotted_for_async_client_creation(self):
        selected = ["www.cdngwc.net"]
        routes = []
        clients = []

        def factory(route):
            routes.append(route)
            client = AsyncClient()
            clients.append(client)
            return client

        service = SearchService(
            api_route_provider=lambda: selected[0],
            query_engine_provider=lambda: "async",
            async_client_factory=factory,
        )
        service.search(SearchRequest(SearchMode.GENERAL, "first"))
        selected[0] = "auto"
        service.search(SearchRequest(SearchMode.GENERAL, "second"))

        self.assertEqual(routes, ["www.cdngwc.net", "auto"])
        self.assertTrue(all(client.close_called == 1 for client in clients))

    def test_setup_and_request_failures_close_without_leaking_details(self):
        for active in (
            AsyncClient(setup_error=TimeoutError("private endpoint")),
            AsyncClient(request_error=TimeoutError("private endpoint")),
        ):
            with self.subTest(setup_error=active.setup_error is not None):
                service = SearchService(
                    async_client_factory=lambda _route, value=active: value,
                )
                with self.assertRaises(SearchUnavailable) as caught:
                    service.search(
                        SearchRequest(SearchMode.GENERAL, "query"),
                        query_engine="async",
                    )
                self.assertNotIn("private endpoint", str(caught.exception))
                self.assertEqual(active.close_called, 1)

    def test_cover_path_remains_sync_even_when_query_default_is_async(self):
        sync = SyncClient()
        sync.get_jm_image = lambda _url: SimpleNamespace(
            http_code=200,
            content=b"cover",
        )
        async_created = []
        service = SearchService(
            client_factory=lambda: sync,
            cover_url_factory=lambda _album_id: "memory-only",
            query_engine_provider=lambda: "async",
            async_client_factory=lambda _route: async_created.append(True),
        )

        self.assertEqual(service.fetch_cover("123"), b"cover")
        self.assertEqual(async_created, [])


if __name__ == "__main__":
    unittest.main()
