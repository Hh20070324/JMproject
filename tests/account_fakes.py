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


class FakeJmAccountClient:
    """Offline fake limited to the JMComic account/favorites contract."""

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

    def get_meta_data(self, name):
        if name == "cookies":
            return dict(self.cookies)
        return None
