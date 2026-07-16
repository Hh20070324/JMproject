import ctypes
import os
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path


CRYPTPROTECT_UI_FORBIDDEN = 0x1


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class CurrentUserDpapiProofTests(unittest.TestCase):
    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    @classmethod
    def setUpClass(cls):
        cls.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        cls.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_pointer = ctypes.POINTER(cls.DataBlob)
        cls.crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        cls.crypt32.CryptProtectData.restype = wintypes.BOOL
        cls.crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        cls.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        cls.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        cls.kernel32.LocalFree.restype = wintypes.HLOCAL

    @classmethod
    def _blob(cls, data: bytes):
        buffer = ctypes.create_string_buffer(data)
        blob = cls.DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return buffer, blob

    @classmethod
    def _copy_and_free(cls, blob):
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            if blob.pbData:
                cls.kernel32.LocalFree(blob.pbData)

    @classmethod
    def _protect(cls, plaintext: bytes) -> bytes:
        input_buffer, input_blob = cls._blob(plaintext)
        output_blob = cls.DataBlob()
        ctypes.set_last_error(0)
        result = cls.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return cls._copy_and_free(output_blob)

    @classmethod
    def _unprotect(cls, ciphertext: bytes) -> bytes:
        input_buffer, input_blob = cls._blob(ciphertext)
        output_blob = cls.DataBlob()
        description = wintypes.LPWSTR()
        ctypes.set_last_error(0)
        result = cls.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if description:
            cls.kernel32.LocalFree(description)
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return cls._copy_and_free(output_blob)

    def test_current_user_round_trip_survives_move_inside_portable_root(self):
        plaintext = b"phase-zero-account-cookie-sentinel"
        ciphertext = self._protect(plaintext)

        self.assertNotEqual(ciphertext, plaintext)
        self.assertNotIn(plaintext, ciphertext)
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "account.dat"
            moved = Path(temp_dir) / "moved-account.dat"
            first.write_bytes(ciphertext)
            first.replace(moved)
            self.assertEqual(self._unprotect(moved.read_bytes()), plaintext)

    def test_ciphertext_tampering_fails_closed_without_system_ui(self):
        ciphertext = bytearray(self._protect(b"tamper-proof-sentinel"))
        ciphertext[len(ciphertext) // 2] ^= 0x01

        with self.assertRaises(OSError):
            self._unprotect(bytes(ciphertext))


if __name__ == "__main__":
    unittest.main()
