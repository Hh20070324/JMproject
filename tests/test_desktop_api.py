import unittest
from unittest.mock import Mock

from desktop import DesktopApi
from jm_downloader.library import LibraryNotFound


class DesktopApiTests(unittest.TestCase):
    def test_uses_native_confirmation_dialog(self):
        api = DesktopApi(Mock())
        window = Mock()
        window.create_confirmation_dialog.return_value = True
        api.set_window(window)

        result = api.confirm("确认", "确定删除吗？")

        self.assertTrue(result)
        window.create_confirmation_dialog.assert_called_once_with(
            "确认", "确定删除吗？"
        )

    def test_opens_library_item_through_desktop_service(self):
        library = Mock()
        api = DesktopApi(library)

        result = api.open_library_item("123", "pdf")

        self.assertEqual(result, {"ok": True})
        library.open_location.assert_called_once_with("123", "pdf")

    def test_returns_library_errors_to_javascript(self):
        library = Mock()
        library.open_location.side_effect = LibraryNotFound("PDF 不存在")
        api = DesktopApi(library)

        result = api.open_library_item("123", "pdf")

        self.assertEqual(result, {"ok": False, "error": "PDF 不存在"})


if __name__ == "__main__":
    unittest.main()
