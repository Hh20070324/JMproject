from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class FakeHttpResponse:
    cookies: dict[str, str]


@dataclass(frozen=True)
class FakeLoginResponse:
    resp: FakeHttpResponse
    res_data: dict[str, object]


@dataclass(frozen=True)
class FakeFavoritePage:
    content: tuple[tuple[str, dict[str, object]], ...]
    folder_list: tuple[dict[str, str], ...]
    total: int
    page_count: int


@dataclass(frozen=True)
class FakeFavoriteAddResponse:
    status: str = "ok"
    type: str = "Add"

    @property
    def model_data(self):
        return self


@dataclass(frozen=True)
class FakeFavoriteFolderMutationResponse:
    status: str = "ok"

    @property
    def model_data(self):
        return self


class FakeJmAccountClient:
    """Offline fake limited to the JMComic account/favorites contract."""

    API_FAVORITE = "/favorite"
    API_FAVORITE_FOLDER = "/favorite_folder"

    def __init__(
        self,
        *,
        username: str = "test-user",
        password: str = "test-password",
        uid: str = "10001",
        cookies: dict[str, str] | None = None,
        folders: dict[
            str,
            tuple[str, tuple[tuple[str, dict[str, object]], ...]],
        ]
        | None = None,
        page_size: int = 2,
    ):
        self.expected_username = username
        self._expected_password = password
        self.uid = uid
        self.login_cookies = dict(cookies or {"session": "test-cookie"})
        self.cookies: dict[str, str] = {}
        self.page_size = page_size
        self.domain_retry_strategy = None
        self.folders = (
            folders
            if folders is not None
            else {
                "0": (
                    "Default",
                    (("1449491", {"name": "First favorite"}),),
                )
            }
        )
        self.calls: list[tuple[object, ...]] = []
        self.login_error: Exception | None = None
        self.favorite_errors: dict[tuple[str, int], Exception] = {}
        self.favorite_add_error: Exception | None = None
        self.favorite_add_response = FakeFavoriteAddResponse()
        self.favorite_folder_mutation_errors: dict[str, Exception] = {}
        self.favorite_folder_mutation_response = (
            FakeFavoriteFolderMutationResponse()
        )

    def login(self, username, password):
        self.calls.append(("login", username))
        if self.login_error is not None:
            raise self.login_error
        if username != self.expected_username or password != self._expected_password:
            raise ValueError("login rejected")

        self.cookies = {**self.login_cookies, "AVS": "test-avs"}
        return FakeLoginResponse(
            FakeHttpResponse(dict(self.login_cookies)),
            {
                "uid": self.uid,
                "username": self.expected_username,
                "s": "test-avs",
            },
        )

    def favorite_folder(
        self,
        page=1,
        order_by="mr",
        folder_id="0",
        username="",
    ):
        folder_id = str(folder_id)
        self.calls.append(
            ("favorite_folder", page, order_by, folder_id, username)
        )
        error = self.favorite_errors.get((folder_id, page))
        if error is not None:
            raise error

        _, items = self.folders[folder_id]
        start = (page - 1) * self.page_size
        content = items[start : start + self.page_size]
        folder_list = tuple(
            {"FID": item_id, "name": name}
            for item_id, (name, _) in self.folders.items()
        )
        return FakeFavoritePage(
            content=tuple(content),
            folder_list=folder_list,
            total=len(items),
            page_count=ceil(len(items) / self.page_size),
        )

    def favorite_folder_gen(
        self,
        page=1,
        order_by="mr",
        folder_id="0",
        username="",
    ):
        while True:
            result = self.favorite_folder(
                page=page,
                order_by=order_by,
                folder_id=folder_id,
                username=username,
            )
            yield result
            if page >= result.page_count:
                return
            page += 1

    def add_favorite_album(self, album_id, folder_id="0"):
        album_id = str(album_id)
        folder_id = str(folder_id)
        self.calls.append(("add_favorite_album", album_id, folder_id))
        if folder_id != "0":
            raise ValueError("fake only supports the default favorite folder")
        if self.favorite_add_error is not None:
            raise self.favorite_add_error

        folder_name, items = self.folders["0"]
        mutation_type = self.favorite_add_response.type.casefold()
        if mutation_type == "add" and all(
            str(item_id) != album_id for item_id, _ in items
        ):
            self.folders["0"] = (
                folder_name,
                items + ((album_id, {"name": None}),),
            )
        elif mutation_type == "remove":
            self.folders["0"] = (
                folder_name,
                tuple(
                    item
                    for item in items
                    if str(item[0]) != album_id
                ),
            )
        return self.favorite_add_response

    def req_api(self, url, get=True, require_success=True, **kwargs):
        if get is not False or require_success is not True:
            raise ValueError("unexpected fake favorite mutation request")
        if set(kwargs) != {"data"} or not isinstance(kwargs["data"], dict):
            raise ValueError("unexpected fake favorite mutation payload")
        data = dict(kwargs["data"])
        if url == self.API_FAVORITE and set(data) == {"aid"}:
            return self.add_favorite_album(data["aid"])
        if url == self.API_FAVORITE_FOLDER:
            return self._mutate_favorite_folder(data)
        raise ValueError("unexpected fake favorite mutation request")

    def _mutate_favorite_folder(self, data):
        mutation_type = data.get("type")
        expected_keys = {
            "add": {"type", "folder_name"},
            "del": {"type", "folder_id"},
            "move": {"type", "aid", "folder_id"},
        }
        if mutation_type not in expected_keys or set(data) != expected_keys[
            mutation_type
        ]:
            raise ValueError("unexpected fake favorite folder payload")
        self.calls.append(
            ("favorite_folder_mutation", mutation_type, dict(data))
        )
        error = self.favorite_folder_mutation_errors.get(mutation_type)
        if error is not None:
            raise error

        if mutation_type == "add":
            numeric_ids = [
                int(folder_id)
                for folder_id in self.folders
                if str(folder_id).isdigit()
            ]
            folder_id = str(max(numeric_ids, default=0) + 1)
            self.folders[folder_id] = (str(data["folder_name"]), ())
        elif mutation_type == "del":
            folder_id = str(data["folder_id"])
            if folder_id == "0" or folder_id not in self.folders:
                raise ValueError("fake cannot delete this favorite folder")
            if self.folders[folder_id][1]:
                raise ValueError("fake cannot delete a non-empty folder")
            del self.folders[folder_id]
        else:
            album_id = str(data["aid"])
            target_id = str(data["folder_id"])
            source_item = None
            for folder_id, (name, items) in tuple(self.folders.items()):
                if folder_id != "0":
                    self.folders[folder_id] = (
                        name,
                        tuple(
                            item
                            for item in items
                            if str(item[0]) != album_id
                        ),
                    )
                if source_item is None:
                    source_item = next(
                        (
                            item
                            for item in items
                            if str(item[0]) == album_id
                        ),
                        None,
                    )
            if target_id:
                if target_id not in self.folders or target_id == "0":
                    raise ValueError("unknown fake favorite target")
                if source_item is None:
                    source_item = (album_id, {"name": None})
                name, items = self.folders[target_id]
                self.folders[target_id] = (name, items + (source_item,))
        return self.favorite_folder_mutation_response

    @staticmethod
    def require_resp_status_ok(response):
        if response.status != "ok":
            raise ValueError("fake favorite mutation rejected")

    def get_meta_data(self, name):
        if name == "cookies":
            return dict(self.cookies)
        return None
