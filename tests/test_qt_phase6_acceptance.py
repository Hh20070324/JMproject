import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PhaseSixAcceptanceTests(unittest.TestCase):
    def test_formal_desktop_entry_uses_qt_main(self):
        import desktop
        from jm_downloader.qt.app import main

        self.assertIs(desktop.main, main)

    def test_runtime_requirements_exclude_legacy_desktop_stack(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).lower()

        for dependency in ("flask", "pywebview", "pythonnet", "werkzeug"):
            with self.subTest(dependency=dependency):
                self.assertNotIn(dependency, requirements)

    def test_qt_smoke_does_not_bind_a_python_socket(self):
        script = textwrap.dedent(
            """
            import socket
            import tempfile
            from pathlib import Path

            original_socket = socket.socket

            class GuardedSocket(original_socket):
                def bind(self, *args, **kwargs):
                    raise AssertionError("desktop startup attempted to bind a socket")

            socket.socket = GuardedSocket

            from jm_downloader.qt.app import run_qt_app
            from jm_downloader.settings import AppPaths

            with tempfile.TemporaryDirectory() as temp_dir:
                result = run_qt_app(
                    ["phase-six-smoke"],
                    smoke_test=True,
                    base_paths=AppPaths(Path(temp_dir)),
                )
            raise SystemExit(result)
            """
        )
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
