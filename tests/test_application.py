import tempfile
import threading
import unittest
from pathlib import Path

from jm_downloader.application import create_app
from jm_downloader.models import TaskSnapshot, TaskStatus
from jm_downloader.settings import AppPaths
from jm_downloader.tasks import InvalidTaskState, TaskManager


class WaitingWorker:
    instances = []

    def __init__(self, album_id, **kwargs):
        self.album_id = album_id
        self.callbacks = kwargs
        self.started = False
        self.stopped = False
        self.wait_timeout = None
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait(self, timeout):
        self.wait_timeout = timeout
        return True


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

        snapshot = self.manager.list_tasks()[0]
        self.assertIsInstance(snapshot, TaskSnapshot)
        self.assertEqual(snapshot.status, TaskStatus.FETCHING)
        self.assertIsNone(snapshot.preview_path)

    def test_add_normalizes_optional_jm_prefix(self):
        response = self.client.post("/api/add", json={"album_id": " JM123456 "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["album_id"], "123456")

    def test_duplicate_active_task_is_rejected(self):
        self.client.post("/api/add", json={"album_id": "123456"})

        response = self.client.post("/api/add", json={"album_id": "123456"})

        self.assertEqual(response.status_code, 409)

    def test_sse_initial_state_keeps_legacy_web_payload(self):
        self.client.post("/api/add", json={"album_id": "123456"})

        response = self.client.get("/api/events", buffered=False)
        first_chunk = next(response.response).decode("utf-8")
        response.close()

        self.assertIn("event: init", first_chunk)
        self.assertIn('"album_id": "123456"', first_chunk)
        self.assertIn('"status": "fetching"', first_chunk)

    def test_sse_converts_core_preview_path_to_controlled_url(self):
        task_id = self.client.post(
            "/api/add", json={"album_id": "123456"}
        ).get_json()["id"]
        response = self.client.get("/api/events", buffered=False)
        next(response.response)
        preview_path = self.paths.pictures / "123456" / "chapter" / "1.jpg"
        preview_path.parent.mkdir(parents=True)
        preview_path.write_bytes(b"preview image")

        WaitingWorker.instances[0].callbacks["on_preview"](
            "123456", str(preview_path)
        )
        event_chunk = next(response.response).decode("utf-8")
        response.close()

        self.assertIn(f"/api/tasks/{task_id}/preview?v=1", event_chunk)
        self.assertNotIn(str(preview_path), event_chunk)

    def test_concurrency_limit_and_remove_pending_task(self):
        task_ids = []
        for album_id in ("1", "2", "3"):
            task_ids.append(
                self.client.post("/api/add", json={"album_id": album_id}).get_json()["id"]
            )

        self.assertEqual(
            [task.status for task in self.manager.list_tasks()],
            [TaskStatus.FETCHING, TaskStatus.FETCHING, TaskStatus.PENDING],
        )

        response = self.client.delete(f"/api/remove/{task_ids[2]}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [task.status for task in self.manager.list_tasks()],
            [TaskStatus.FETCHING, TaskStatus.FETCHING],
        )

    def test_active_task_cannot_be_removed_as_if_it_stopped_immediately(self):
        task_id = self.client.post(
            "/api/add", json={"album_id": "1"}
        ).get_json()["id"]

        response = self.client.delete(f"/api/remove/{task_id}")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(WaitingWorker.instances[0].stopped)

    def test_completed_worker_schedules_next_pending_task(self):
        for album_id in ("1", "2", "3"):
            self.client.post("/api/add", json={"album_id": album_id})
        pdf_path = self.paths.pdfs / "1.pdf"
        pdf_path.write_bytes(b"pdf")

        WaitingWorker.instances[0].callbacks["on_complete"]("1", str(pdf_path))

        self.assertEqual(len(WaitingWorker.instances), 3)
        self.assertEqual(
            [task.status for task in self.manager.list_tasks()],
            [TaskStatus.COMPLETED, TaskStatus.FETCHING, TaskStatus.FETCHING],
        )

    def test_stale_worker_callback_does_not_replace_retry_generation(self):
        task_id = self.client.post(
            "/api/add", json={"album_id": "1"}
        ).get_json()["id"]
        original = WaitingWorker.instances[0]
        original.callbacks["on_error"]("1", "first failure")

        self.client.post(f"/api/retry/{task_id}")
        replacement = WaitingWorker.instances[1]
        original.callbacks["on_error"]("1", "late stale failure")

        snapshot = self.manager.get_task(task_id)
        self.assertEqual(snapshot.status, TaskStatus.FETCHING)
        self.assertIsNone(snapshot.error)
        self.assertTrue(self.manager.shutdown(timeout=1))
        self.assertTrue(replacement.stopped)
        self.assertIsNotNone(replacement.wait_timeout)

    def test_stop_all_notifies_active_workers(self):
        self.client.post("/api/add", json={"album_id": "1"})
        self.client.post("/api/add", json={"album_id": "2"})

        self.assertTrue(self.manager.has_active_tasks())
        self.manager.stop_all()

        self.assertTrue(all(worker.stopped for worker in WaitingWorker.instances))

    def test_shutdown_waits_for_workers_and_stops_new_scheduling(self):
        self.client.post("/api/add", json={"album_id": "1"})

        self.assertTrue(self.manager.shutdown(timeout=1))
        worker = WaitingWorker.instances[0]
        self.assertTrue(worker.stopped)
        self.assertIsNotNone(worker.wait_timeout)
        with self.assertRaises(InvalidTaskState):
            self.manager.add("2")

    def test_shutdown_during_worker_creation_prevents_late_start(self):
        factory_entered = threading.Event()
        release_factory = threading.Event()

        def blocking_factory(album_id, **kwargs):
            factory_entered.set()
            release_factory.wait(timeout=2)
            return WaitingWorker(album_id, **kwargs)

        manager = TaskManager(paths=self.paths, worker_factory=blocking_factory)
        errors = []

        def add_task():
            try:
                manager.add("1")
            except Exception as error:
                errors.append(error)

        add_thread = threading.Thread(target=add_task)
        add_thread.start()
        self.assertTrue(factory_entered.wait(timeout=1))

        self.assertTrue(manager.shutdown(timeout=0))
        release_factory.set()
        add_thread.join(timeout=2)

        self.assertFalse(add_thread.is_alive())
        self.assertEqual(errors, [])
        worker = WaitingWorker.instances[-1]
        self.assertFalse(worker.started)
        self.assertTrue(worker.stopped)

    def test_shutdown_keeps_timed_out_worker_tracked_for_second_wait(self):
        class DelayedWorker(WaitingWorker):
            def __init__(self, album_id, **kwargs):
                super().__init__(album_id, **kwargs)
                self.wait_count = 0

            def wait(self, timeout):
                self.wait_timeout = timeout
                self.wait_count += 1
                return self.wait_count >= 2

        manager = TaskManager(paths=self.paths, worker_factory=DelayedWorker)
        manager.add("1")
        worker = WaitingWorker.instances[-1]

        self.assertFalse(manager.shutdown(timeout=0))
        self.assertTrue(manager.shutdown(timeout=0))
        self.assertEqual(worker.wait_count, 2)

    def test_preview_event_exposes_only_a_controlled_image_route(self):
        task_id = self.client.post(
            "/api/add", json={"album_id": "123456"}
        ).get_json()["id"]
        preview_path = self.paths.pictures / "123456" / "chapter" / "1.jpg"
        preview_path.parent.mkdir(parents=True)
        preview_path.write_bytes(b"preview image")
        listener = self.manager.add_listener()

        WaitingWorker.instances[0].callbacks["on_preview"](
            "123456", str(preview_path)
        )

        event = listener.get_nowait()
        self.manager.remove_listener(listener)
        self.assertEqual(event["preview_path"], preview_path.resolve())
        self.assertNotIn("preview", event)
        snapshot = self.manager.get_task(task_id)
        self.assertEqual(snapshot.preview_path, preview_path.resolve())
        self.assertEqual(snapshot.preview_revision, 1)
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
        item = response.get_json()[0]
        self.assertEqual(item["album_id"], "99")
        self.assertEqual(item["preview"], "/api/library/99/preview")
        self.assertNotIn(str(image_path), response.get_data(as_text=True))
        preview = self.client.get("/api/library/99/preview")
        self.assertEqual(preview.data, b"image")
        preview.close()

    def test_completed_task_exposes_controlled_pdf_url(self):
        task_id = self.client.post(
            "/api/add", json={"album_id": "123456"}
        ).get_json()["id"]
        pdf_path = self.paths.pdfs / "123456.pdf"
        pdf_path.write_bytes(b"pdf")
        listener = self.manager.add_listener()

        WaitingWorker.instances[0].callbacks["on_complete"](
            "123456", str(pdf_path)
        )

        event = listener.get_nowait()
        self.manager.remove_listener(listener)
        self.assertEqual(event["pdf_path"], pdf_path.resolve())
        self.assertNotIn("pdf", event)
        snapshot = self.manager.get_task(task_id)
        self.assertEqual(snapshot.pdf_path, pdf_path.resolve())
        task = self.client.get("/api/queue").get_json()[0]
        self.assertEqual(task["pdf"], "/api/library/123456/pdf")
        self.assertNotIn(
            str(pdf_path), self.client.get("/api/queue").get_data(as_text=True)
        )

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
