import base64
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import jmcomic

from jm_downloader.models import (
    ChapterCatalogSnapshot,
    ChapterSnapshot,
    ReaderErrorKind,
    ReaderPageState,
)
from jm_downloader.reader import (
    ReaderDiskCache,
    ReaderService,
    ReaderServiceError,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
    "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeImageResponse:
    def __init__(self, content=PNG):
        self.content = content
        self.transfer_calls = []

    def transfer_to(
        self,
        path,
        scramble_id,
        decode_image=True,
        img_url=None,
    ):
        self.transfer_calls.append(
            (path, scramble_id, decode_image, img_url)
        )
        Path(path).write_bytes(self.content)


class FakeReaderClient:
    def __init__(
        self,
        *,
        album=None,
        photo=None,
        image_behavior=None,
    ):
        self.album = album
        self.photo = photo
        self.image_behavior = image_behavior or (
            lambda _url: FakeImageResponse()
        )
        self.setup_called = 0
        self.closed = False
        self.domains = None
        self.image_calls = 0

    async def setup(self):
        self.setup_called += 1

    async def close(self):
        self.closed = True

    def set_domain_list(self, domains):
        self.domains = list(domains)

    async def get_album_detail(self, _album_id):
        if isinstance(self.album, Exception):
            raise self.album
        return self.album

    async def get_photo_detail(
        self,
        _photo_id,
        fetch_album=False,
        fetch_scramble_id=True,
    ):
        if isinstance(self.photo, Exception):
            raise self.photo
        return self.photo

    async def get_jm_image(self, url):
        self.image_calls += 1
        result = self.image_behavior(url)
        if isinstance(result, Exception):
            raise result
        return result


def make_album():
    return SimpleNamespace(
        album_id="123",
        title="测试漫画",
        episode_list=[
            ("301", 1, "第 1 章"),
            ("302", 2, "第 2 章"),
        ],
    )


def make_photo(photo_id="301", pages=None):
    return jmcomic.JmPhotoDetail(
        photo_id,
        "第 1 章",
        "123",
        1,
        scramble_id="220980",
        page_arr=pages or ["001.png", "002.png"],
        data_original_domain="cdn.invalid",
        data_original_0=f"/media/photos/{photo_id}/",
    )


class ReaderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.cache = ReaderDiskCache(
            root / "ReaderTemp",
            budget_bytes=1024 * 1024,
        )
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            if not client.closed:
                await client.close()
        self.cache.close()
        self.temporary.cleanup()

    def make_service(self, client_factory, **kwargs):
        return ReaderService(
            option_file=Path(self.temporary.name) / "option.yml",
            disk_cache=self.cache,
            client_factory=client_factory,
            retry_delays=(0, 0),
            **kwargs,
        )

    async def test_catalog_chapter_and_page_snapshots_hide_urls(self):
        client = FakeReaderClient(album=make_album(), photo=make_photo())
        self.clients.append(client)
        service = self.make_service(lambda _cookies, _route: client)

        catalog = await service.fetch_catalog("JM00123")
        chapter, pages = await service.load_chapter(catalog, "301")
        key, ready = await service.fetch_page(
            "301",
            1,
            current_page=1,
        )

        self.assertIsInstance(catalog, ChapterCatalogSnapshot)
        self.assertEqual(chapter.page_count, 2)
        self.assertEqual(pages[0].state, ReaderPageState.PLACEHOLDER)
        self.assertEqual(ready.state, ReaderPageState.READY)
        self.assertEqual((ready.width, ready.height), (1, 1))
        self.assertTrue(ready.cache_path.is_file())
        self.assertEqual(self.cache.path_for(key), ready.cache_path)
        combined = repr((catalog, chapter, pages, ready))
        self.assertNotIn("cdn.invalid", combined)
        self.assertNotIn("https://", combined)
        await service.close()
        self.assertTrue(client.closed)

    async def test_network_retry_is_bounded_and_other_page_can_succeed(self):
        outcomes = [
            TimeoutError("one"),
            TimeoutError("two"),
            FakeImageResponse(),
        ]
        client = FakeReaderClient(
            album=make_album(),
            photo=make_photo(),
            image_behavior=lambda _url: outcomes.pop(0),
        )
        self.clients.append(client)
        service = self.make_service(lambda _cookies, _route: client)
        catalog = await service.fetch_catalog("123")
        await service.load_chapter(catalog, "301")

        _key, page = await service.fetch_page(
            "301",
            1,
            current_page=1,
        )

        self.assertEqual(page.state, ReaderPageState.READY)
        self.assertEqual(client.image_calls, 3)

        _cached_key, cached_page = await service.fetch_page(
            "301",
            1,
            current_page=1,
        )
        self.assertEqual(cached_page.state, ReaderPageState.READY)
        self.assertEqual(client.image_calls, 3)

    async def test_oversize_and_damaged_pages_have_stable_categories(self):
        responses = {
            "001.png": FakeImageResponse(b"x" * 11),
            "002.png": FakeImageResponse(b"bad"),
        }
        client = FakeReaderClient(
            album=make_album(),
            photo=make_photo(),
            image_behavior=lambda url: responses[url.rsplit("/", 1)[-1]],
        )
        self.clients.append(client)
        service = self.make_service(
            lambda _cookies, _route: client,
            max_response_bytes=10,
        )
        catalog = await service.fetch_catalog("123")
        await service.load_chapter(catalog, "301")

        with self.assertRaises(ReaderServiceError) as oversize:
            await service.fetch_page("301", 1, current_page=1)
        with self.assertRaises(ReaderServiceError) as damaged:
            await service.fetch_page("301", 2, current_page=2)

        self.assertEqual(
            oversize.exception.kind,
            ReaderErrorKind.IMAGE_TOO_LARGE,
        )
        self.assertEqual(
            damaged.exception.kind,
            ReaderErrorKind.IMAGE_DAMAGED,
        )
        self.assertEqual(self.cache.total_bytes, 0)

    async def test_expired_authenticated_client_falls_back_once(self):
        expired = FakeReaderClient(album=PermissionError("expired"))
        anonymous = FakeReaderClient(album=make_album())
        self.clients.extend((expired, anonymous))
        calls = []
        marked = []

        def factory(cookies, route):
            calls.append((cookies, route))
            return expired if cookies else anonymous

        service = self.make_service(
            factory,
            session_cookie_provider=lambda: {"AVS": "secret"},
            session_expired_callback=lambda: marked.append(True),
        )

        catalog = await service.fetch_catalog("123")

        self.assertEqual(catalog.album_id, "123")
        self.assertEqual(
            calls,
            [({"AVS": "secret"}, "auto"), (None, "auto")],
        )
        self.assertEqual(marked, [True])
        self.assertTrue(expired.closed)

    async def test_fixed_route_failure_has_route_category(self):
        client = FakeReaderClient(album=TimeoutError("offline"))
        self.clients.append(client)
        service = self.make_service(
            lambda _cookies, _route: client,
            api_route_provider=lambda: "www.cdnhjk.net",
        )

        with self.assertRaises(ReaderServiceError) as caught:
            await service.fetch_catalog("123")

        self.assertEqual(
            caught.exception.kind,
            ReaderErrorKind.ROUTE_UNAVAILABLE,
        )
        self.assertEqual(client.domains, ["www.cdnhjk.net"])

    async def test_missing_or_invalid_chapter_never_requests_images(self):
        client = FakeReaderClient(album=make_album(), photo=make_photo())
        self.clients.append(client)
        service = self.make_service(lambda _cookies, _route: client)
        catalog = await service.fetch_catalog("123")

        with self.assertRaises(ReaderServiceError) as caught:
            await service.load_chapter(catalog, "999")

        self.assertEqual(
            caught.exception.kind,
            ReaderErrorKind.CHAPTER_UNAVAILABLE,
        )
        self.assertEqual(client.image_calls, 0)


if __name__ == "__main__":
    unittest.main()
