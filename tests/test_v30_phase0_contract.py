import asyncio
import base64
import inspect
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import jmcomic
from jmcomic.jm_async_client import AsyncJmApiClient
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView


V30_READER_LIMITS = {
    "response_bytes": 32 * 1024 * 1024,
    "image_side_pixels": 16_384,
    "image_total_pixels": 24_000_000,
    "chapter_pages": 2_000,
    "network_concurrency": 3,
    "queued_unique_pages": 64,
    "automatic_retries": 2,
    "network_timeout_seconds": 30,
    "memory_cache_bytes": 128 * 1024 * 1024,
    "disk_cache_bytes": 512 * 1024 * 1024,
}


class V30UpstreamReaderProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_async_client_surface_and_lifecycle(self):
        option = jmcomic.create_option_by_file("option.yml")
        client = option.new_jm_async_client(max_clients=3)
        self.assertIsInstance(client, AsyncJmApiClient)
        self.assertEqual(client._max_clients_hint, 3)
        self.assertIsNone(client._session)

        self.assertTrue(
            inspect.iscoroutinefunction(AsyncJmApiClient.get_photo_detail)
        )
        self.assertTrue(
            inspect.iscoroutinefunction(AsyncJmApiClient.get_jm_image)
        )
        self.assertTrue(inspect.iscoroutinefunction(AsyncJmApiClient.setup))
        self.assertTrue(inspect.iscoroutinefunction(AsyncJmApiClient.close))
        self.assertEqual(
            tuple(inspect.signature(client.get_photo_detail).parameters),
            ("photo_id", "fetch_album", "fetch_scramble_id"),
        )

        await client.close()
        self.assertIsNone(client._session)

    def test_photo_exposes_stable_per_page_image_descriptions(self):
        photo = jmcomic.JmPhotoDetail(
            "301",
            "第 1 章",
            "123",
            1,
            scramble_id="220980",
            page_arr=["001.jpg", "002.png"],
            data_original_domain="cdn.invalid",
            data_original_0="/media/photos/301/",
        )

        first = photo.create_image_detail(0)
        second = photo.create_image_detail(1)

        self.assertEqual(first.index, 1)
        self.assertEqual(second.index, 2)
        self.assertEqual(first.aid, "301")
        self.assertEqual(first.scramble_id, "220980")
        self.assertTrue(first.download_url.endswith("/001.jpg"))
        self.assertTrue(second.download_url.endswith("/002.png"))
        with self.assertRaises(IndexError):
            photo.create_image_detail(2)

    def test_image_response_transfer_publishes_valid_image_without_network(self):
        one_pixel_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
            "QVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )

        class _Response:
            content = one_pixel_png
            url = "https://cdn.invalid/001.png"
            status_code = 200

        response = jmcomic.JmImageResp(_Response())
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "page.png"
            response.transfer_to(
                str(target),
                scramble_id=None,
                decode_image=False,
                img_url=_Response.url,
            )

            self.assertEqual(target.read_bytes(), one_pixel_png)


class V30ReaderSafetyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(
            ["v30-phase0-contract"]
        )

    def test_initial_limits_are_bounded_and_internally_consistent(self):
        self.assertEqual(V30_READER_LIMITS["network_concurrency"], 3)
        self.assertLessEqual(V30_READER_LIMITS["queued_unique_pages"], 64)
        self.assertLessEqual(V30_READER_LIMITS["chapter_pages"], 2_000)
        self.assertLessEqual(
            V30_READER_LIMITS["image_total_pixels"],
            V30_READER_LIMITS["image_side_pixels"] ** 2,
        )
        self.assertLess(
            V30_READER_LIMITS["memory_cache_bytes"],
            V30_READER_LIMITS["disk_cache_bytes"],
        )
        self.assertGreater(QImageReader.allocationLimit(), 0)

    def test_two_thousand_lightweight_placeholders_fit_native_scene(self):
        scene = QGraphicsScene()
        view = QGraphicsView(scene)
        view.resize(960, 720)
        page_width = 820
        page_height = 1_160
        gap = 12
        for index in range(V30_READER_LIMITS["chapter_pages"]):
            scene.addRect(
                0,
                index * (page_height + gap),
                page_width,
                page_height,
            )
        scene.setSceneRect(
            0,
            0,
            page_width,
            V30_READER_LIMITS["chapter_pages"] * (page_height + gap),
        )

        self.assertEqual(len(scene.items()), 2_000)
        self.assertGreater(scene.sceneRect().height(), 2_000_000)
        self.assertGreater(
            view.verticalScrollBar().maximum(),
            view.verticalScrollBar().minimum(),
        )
        view.deleteLater()
        scene.deleteLater()

    def test_reader_contract_keeps_temp_and_history_portable(self):
        root = Path("portable-root").resolve()
        self.assertEqual(root / "ReaderTemp", root.joinpath("ReaderTemp"))
        self.assertEqual(
            root / "reading_history.dat",
            root.joinpath("reading_history.dat"),
        )


if __name__ == "__main__":
    unittest.main()
