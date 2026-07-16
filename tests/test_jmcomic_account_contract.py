import inspect
import unittest
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jmcomic

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


if __name__ == "__main__":
    unittest.main()
