import tempfile
import unittest
from pathlib import Path

from jm_downloader.application import create_app
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import TaskManager


class WaitingWorker:
    instances = []

    def __init__(self, album_id, **kwargs):
        self.album_id = album_id
        self.callbacks = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        WaitingWorker.instances = []
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp_dir.name))
        self.paths.web.mkdir()
        (self.paths.web / "index.html").write_text("test", encoding="utf-8")
        self.manager = TaskManager(paths=self.paths, worker_factory=WaitingWorker)
        self.app = create_app(paths=self.paths, manager=self.manager)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_rejects_non_numeric_album_ids(self):
        response = self.client.post("/api/add", json={"album_id": "12/34"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "车号只能包含数字"})
        self.assertEqual(self.manager.list_tasks(), [])

    def test_add_and_list_task(self):
        response = self.client.post("/api/add", json={"album_id": "123456"})

        self.assertEqual(response.status_code, 200)
        task = self.client.get("/api/queue").get_json()[0]
        self.assertEqual(task["album_id"], "123456")
        self.assertEqual(task["status"], "fetching")

    def test_duplicate_active_task_is_rejected(self):
        self.client.post("/api/add", json={"album_id": "123456"})

        response = self.client.post("/api/add", json={"album_id": "123456"})

        self.assertEqual(response.status_code, 409)

    def test_concurrency_limit_and_remove_schedule_next_task(self):
        task_ids = []
        for album_id in ("1", "2", "3"):
            task_ids.append(
                self.client.post("/api/add", json={"album_id": album_id}).get_json()["id"]
            )

        self.assertEqual(
            [task["status"] for task in self.manager.list_tasks()],
            ["fetching", "fetching", "pending"],
        )

        response = self.client.delete(f"/api/remove/{task_ids[0]}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WaitingWorker.instances[0].stopped)
        self.assertEqual(
            [task["status"] for task in self.manager.list_tasks()],
            ["fetching", "fetching"],
        )

    def test_stop_all_notifies_active_workers(self):
        self.client.post("/api/add", json={"album_id": "1"})
        self.client.post("/api/add", json={"album_id": "2"})

        self.assertTrue(self.manager.has_active_tasks())
        self.manager.stop_all()

        self.assertTrue(all(worker.stopped for worker in WaitingWorker.instances))

    def test_preview_event_exposes_only_a_controlled_image_route(self):
        task_id = self.client.post(
            "/api/add", json={"album_id": "123456"}
        ).get_json()["id"]
        preview_path = self.paths.pictures / "123456" / "chapter" / "1.jpg"
        preview_path.parent.mkdir(parents=True)
        preview_path.write_bytes(b"preview image")

        WaitingWorker.instances[0].callbacks["on_preview"](
            "123456", str(preview_path)
        )

        task = self.client.get("/api/queue").get_json()[0]
        self.assertEqual(
            task["preview"], f"/api/tasks/{task_id}/preview?v=1"
        )
        self.assertNotIn(str(preview_path), self.client.get("/api/queue").get_data(as_text=True))
        response = self.client.get(f"/api/tasks/{task_id}/preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"preview image")
        response.close()

    def test_library_api_reads_existing_files(self):
        image_path = self.paths.pictures / "99" / "1" / "1.jpg"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"image")

        response = self.client.get("/api/library")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["album_id"], "99")
        preview = self.client.get("/api/library/99/preview")
        self.assertEqual(preview.data, b"image")
        preview.close()

    def test_library_mutation_is_blocked_during_download(self):
        image_path = self.paths.pictures / "99" / "1" / "1.jpg"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"image")
        self.client.post("/api/add", json={"album_id": "99"})

        response = self.client.delete("/api/library/99/images")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(image_path.is_file())


if __name__ == "__main__":
    unittest.main()
