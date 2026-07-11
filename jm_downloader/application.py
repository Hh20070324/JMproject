from flask import Flask, send_from_directory

from .routes import create_api_blueprint
from .library import LibraryService
from .settings import AppPaths, DEFAULT_PATHS
from .tasks import TaskManager


def create_app(
    paths: AppPaths = DEFAULT_PATHS,
    manager: TaskManager | None = None,
    library: LibraryService | None = None,
) -> Flask:
    paths.ensure_output_directories()
    task_manager = manager or TaskManager(paths=paths)
    library_service = library or LibraryService(paths=paths)
    app = Flask(__name__, static_folder=str(paths.web), static_url_path="")
    app.config["TASK_MANAGER"] = task_manager
    app.config["LIBRARY_SERVICE"] = library_service
    app.register_blueprint(create_api_blueprint(task_manager, library_service))

    @app.get("/")
    def index():
        return send_from_directory(paths.web, "index.html")

    return app
