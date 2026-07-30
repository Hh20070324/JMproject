from collections.abc import Callable
from pathlib import Path
import threading
import time

import jmcomic

from .jmcomic_client import serialized_client_construction
from .models import API_ROUTES, QUERY_ENGINES


API_ROUTE_LABELS = {
    "auto": "自动选择",
    "www.cdnhjk.net": "路线 1",
    "www.cdngwc.cc": "路线 2",
    "www.cdngwc.net": "路线 3",
    "www.cdngwc.club": "路线 4",
}


def validate_api_route(route: str) -> str:
    if not isinstance(route, str) or route not in API_ROUTES:
        raise ValueError("API 路线无效")
    return route


def apply_api_route(option, route: str):
    route = validate_api_route(route)
    if route == "auto":
        return option
    option.client.domain = [route]
    return option


class ApiRouteState:
    def __init__(self, route: str = "auto"):
        self._lock = threading.Lock()
        self._route = validate_api_route(route)

    def get(self) -> str:
        with self._lock:
            return self._route

    def set(self, route: str) -> None:
        route = validate_api_route(route)
        with self._lock:
            self._route = route


def validate_query_engine(engine: str) -> str:
    if not isinstance(engine, str) or engine not in QUERY_ENGINES:
        raise ValueError("查询与同步引擎无效")
    return engine


class QueryEngineState:
    def __init__(self, engine: str = "async"):
        self._lock = threading.Lock()
        self._engine = validate_query_engine(engine)

    def get(self) -> str:
        with self._lock:
            return self._engine

    def set(self, engine: str) -> None:
        engine = validate_query_engine(engine)
        with self._lock:
            self._engine = engine


def probe_api_route(
    option_file: Path,
    route: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    route = validate_api_route(route)
    started = clock()
    with serialized_client_construction():
        option = jmcomic.create_option_by_file(str(option_file))
        option.client.retry_times = 0
        option.client.postman.meta_data.timeout = 8
        apply_api_route(option, route)
        client = option.new_jm_client(
            impl="api",
            timeout=8,
        )
    client.get_album_detail("1")
    return max(0, int((clock() - started) * 1000))
