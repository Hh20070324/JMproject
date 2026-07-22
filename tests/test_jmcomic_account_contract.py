import inspect
import unittest
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jmcomic

from jm_downloader.favorites import (
    _invoke_add_favorite,
    _invoke_favorite_folder_mutation,
)
from tests.account_fakes import FakeJmAccountClient


class JmcomicAccountContractTests(unittest.TestCase):
    def test_runtime_dependency_is_the_reviewed_jmcomic_version(self):
        self.assertEqual(version("jmcomic"), "2.7.1")

    def test_account_method_signatures_match_the_reviewed_contract(self):
        self.assertEqual(
            tuple(inspect.signature(jmcomic.JmApiClient.login).parameters),
            ("self", "username", "password"),
        )
        favorite = inspect.signature(jmcomic.JmApiClient.favorite_folder)
        self.assertEqual(
            tuple(favorite.parameters),
            ("self", "page", "order_by", "folder_id", "username"),
        )
        self.assertEqual(favorite.parameters["page"].default, 1)
        self.assertEqual(favorite.parameters["folder_id"].default, "0")
        add_favorite = inspect.signature(
            jmcomic.JmApiClient.add_favorite_album
        )
        self.assertEqual(
            tuple(add_favorite.parameters),
            ("self", "album_id", "folder_id"),
        )
        self.assertEqual(add_favorite.parameters["folder_id"].default, "0")

    def test_login_posts_credentials_and_promotes_response_cookies(self):
        response = SimpleNamespace(
            resp=SimpleNamespace(cookies={"session": "cookie-token"}),
            res_data={"s": "avs-token", "uid": "42"},
        )

        class ContractClient(dict):
            def __init__(self):
                super().__init__()
                self.calls = []

            def req_api(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return response

        client = ContractClient()
        result = jmcomic.JmApiClient.login(client, "account", "password")

        self.assertIs(result, response)
        self.assertEqual(
            client.calls,
            [
                (
                    ("/login", False),
                    {
                        "data": {
                            "username": "account",
                            "password": "password",
                        }
                    },
                )
            ],
        )
        self.assertEqual(
            client["cookies"],
            {"session": "cookie-token", "AVS": "avs-token"},
        )

    def test_favorite_folder_uses_api_parser_and_expected_parameters(self):
        model_data = object()
        response = SimpleNamespace(model_data=model_data)

        class ContractClient:
            API_FAVORITE = "/favorite"

            def __init__(self):
                self.calls = []

            def req_api(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return response

        client = ContractClient()
        parsed = object()
        with patch.object(
            jmcomic.JmPageTool,
            "parse_api_to_favorite_page",
            return_value=parsed,
        ) as parser:
            result = jmcomic.JmApiClient.favorite_folder(
                client,
                page=3,
                order_by="mr",
                folder_id="7",
            )

        self.assertIs(result, parsed)
        self.assertEqual(
            client.calls,
            [
                (
                    ("/favorite",),
                    {"params": {"page": 3, "folder_id": "7", "o": "mr"}},
                )
            ],
        )
        parser.assert_called_once_with(model_data)

    def test_favorite_generator_delegates_pagination_parameters(self):
        sentinel = object()

        class ContractClient:
            favorite_folder = Mock()

            def __init__(self):
                self.calls = []

            def do_page_iter(self, params, page, method):
                self.calls.append((params, page, method))
                yield sentinel

        client = ContractClient()
        result = list(
            jmcomic.JmApiClient.favorite_folder_gen(
                client,
                page=2,
                folder_id="9",
                username="ignored-upstream",
            )
        )

        self.assertEqual(result, [sentinel])
        self.assertEqual(
            client.calls,
            [
                (
                    {
                        "order_by": "mr",
                        "folder_id": "9",
                        "username": "ignored-upstream",
                    },
                    2,
                    client.favorite_folder,
                )
            ],
        )

    def test_add_favorite_uses_aid_and_the_api_default_folder(self):
        response = object()

        class ContractClient:
            def __init__(self):
                self.calls = []
                self.validated = []

            def req_api(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return response

            def require_resp_status_ok(self, value):
                self.validated.append(value)

        client = ContractClient()
        result = jmcomic.JmApiClient.add_favorite_album(
            client,
            "1449491",
            folder_id="99",
        )

        self.assertIs(result, response)
        self.assertEqual(
            client.calls,
            [(("/favorite",), {"data": {"aid": "1449491"}})],
        )
        self.assertEqual(client.validated, [response])

    def test_add_favorite_propagates_the_status_failure_for_sanitizing(self):
        response = object()
        upstream_error = RuntimeError(
            "upstream-response-and-url-must-not-reach-the-ui"
        )

        class ContractClient:
            def req_api(self, *_args, **_kwargs):
                return response

            def require_resp_status_ok(self, value):
                self.validated = value
                raise upstream_error

        client = ContractClient()
        with self.assertRaises(RuntimeError) as raised:
            jmcomic.JmApiClient.add_favorite_album(client, "1449491")

        self.assertIs(raised.exception, upstream_error)
        self.assertIs(client.validated, response)

    def test_favorite_mutation_workaround_dispatches_one_post(self):
        response = SimpleNamespace(model_data={"type": "Add"})

        class ContractClient:
            API_FAVORITE = "/favorite"

            def __init__(self):
                self.calls = []

            def req_api(self, *args, **kwargs):
                self.calls.append(("req_api", args, kwargs))
                return response

            def require_resp_status_ok(self, value):
                self.calls.append(("require_resp_status_ok", value))

        client = ContractClient()

        mutation_type = _invoke_add_favorite(client, "1449491")

        self.assertEqual(mutation_type, "add")
        self.assertEqual(
            client.calls,
            [
                (
                    "req_api",
                    ("/favorite",),
                    {"get": False, "data": {"aid": "1449491"}},
                ),
                ("require_resp_status_ok", response),
            ],
        )

    def test_favorite_folder_mutation_dispatches_one_reviewed_post(self):
        response = SimpleNamespace(status="ok")

        class ContractClient:
            def __init__(self):
                self.calls = []

            def req_api(self, *args, **kwargs):
                self.calls.append(("req_api", args, kwargs))
                return response

            def require_resp_status_ok(self, value):
                self.calls.append(("require_resp_status_ok", value))

        client = ContractClient()
        payload = {"type": "move", "aid": "1449491", "folder_id": "8"}

        result = _invoke_favorite_folder_mutation(client, payload)

        self.assertIsNone(result)
        self.assertEqual(
            client.calls,
            [
                (
                    "req_api",
                    ("/favorite_folder",),
                    {"get": False, "data": payload},
                ),
                ("require_resp_status_ok", response),
            ],
        )

    def test_zero_request_retries_stops_after_the_first_failure(self):
        upstream_error = TimeoutError("single-attempt-sentinel")

        class RetryContractClient:
            retry_times = 0
            domain_retry_strategy = None
            domain_list = ["example.invalid"]

            @staticmethod
            def of_api_url(path, domain):
                return f"https://{domain}{path}"

            @staticmethod
            def update_request_with_specify_domain(
                _kwargs,
                _domain,
                _is_image,
            ):
                return None

            @staticmethod
            def log_topic():
                return "contract"

            @staticmethod
            def decode(value):
                return value

            @staticmethod
            def raise_if_resp_should_retry(response, _is_image):
                return response

        attempts = []

        def fail_once(url, **kwargs):
            attempts.append((url, kwargs))
            raise upstream_error

        client = RetryContractClient()
        with (
            patch("jmcomic.jm_client_impl.jm_log"),
            self.assertRaises(TimeoutError) as raised,
        ):
            jmcomic.JmApiClient.request_with_retry(
                client,
                fail_once,
                "/favorite",
                0,
                0,
                False,
                data={"aid": "1449491"},
            )

        self.assertIs(raised.exception, upstream_error)
        self.assertEqual(
            attempts,
            [
                (
                    "https://example.invalid/favorite",
                    {"data": {"aid": "1449491"}},
                )
            ],
        )

    def test_offline_fake_preserves_folders_pages_and_call_shape(self):
        folders = {
            "0": (
                "Default",
                (
                    ("1", {"name": "One"}),
                    ("2", {"name": "Two"}),
                    ("3", {"name": "Three"}),
                ),
            ),
            "8": ("Second folder", (("4", {"name": "Four"}),)),
        }
        client = FakeJmAccountClient(folders=folders, page_size=2)

        login = client.login("test-user", "test-password")
        pages = list(client.favorite_folder_gen(folder_id="0"))

        self.assertEqual(login.res_data["uid"], "10001")
        self.assertEqual(client.cookies["AVS"], "test-avs")
        self.assertEqual([page.page_count for page in pages], [2, 2])
        self.assertEqual(
            [item[0] for page in pages for item in page.content],
            ["1", "2", "3"],
        )
        self.assertEqual(
            pages[0].folder_list,
            (
                {"FID": "0", "name": "Default"},
                {"FID": "8", "name": "Second folder"},
            ),
        )
        self.assertEqual(client.calls[0], ("login", "test-user"))

    def test_offline_fake_can_represent_an_empty_favorite_folder(self):
        client = FakeJmAccountClient(
            folders={"0": ("Default", ())},
            page_size=2,
        )

        page = client.favorite_folder(folder_id="0")

        self.assertEqual(page.content, ())
        self.assertEqual(page.total, 0)
        self.assertEqual(page.page_count, 0)

    def test_offline_fake_adds_only_to_the_default_folder(self):
        client = FakeJmAccountClient(
            folders={"0": ("Default", ())},
            page_size=2,
        )

        response = client.add_favorite_album("350234")
        page = client.favorite_folder(folder_id="0")

        self.assertEqual(response.status, "ok")
        self.assertEqual(
            client.calls[0],
            ("add_favorite_album", "350234", "0"),
        )
        self.assertEqual(page.content[0][0], "350234")
        with self.assertRaises(ValueError):
            client.add_favorite_album("350235", folder_id="8")

    def test_offline_fake_can_expose_an_add_failure(self):
        client = FakeJmAccountClient()
        upstream_error = RuntimeError("fake-upstream-sentinel")
        client.favorite_add_error = upstream_error

        with self.assertRaises(RuntimeError) as raised:
            client.add_favorite_album("350234")

        self.assertIs(raised.exception, upstream_error)


if __name__ == "__main__":
    unittest.main()
