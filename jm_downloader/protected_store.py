import base64
import binascii
from collections.abc import Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Protocol

from .settings import AppPaths, DEFAULT_PATHS


ENVELOPE_FORMAT = "jm-downloader.protected"
ENVELOPE_SCHEMA_VERSION = 1
CRYPTPROTECT_UI_FORBIDDEN = 0x1

ACCOUNT_MAX_PLAINTEXT_BYTES = 256 * 1024
FAVORITES_MAX_PLAINTEXT_BYTES = 32 * 1024 * 1024
DPAPI_MAX_OVERHEAD_BYTES = 64 * 1024


class ProtectedStoreError(Exception):
    pass


class ProtectedStorePathError(ProtectedStoreError):
    pass


class ProtectedStoreValidationError(ProtectedStoreError):
    pass


class ProtectedStoreUnreadableError(ProtectedStoreError):
    pass


class ProtectedStoreWriteError(ProtectedStoreError):
    pass


class ProtectedStoreDeleteError(ProtectedStoreError):
    pass


class UnsupportedProtectedStoreVersion(ProtectedStoreError):
    pass


class UnsupportedProtectedPayloadVersion(ProtectedStoreError):
    pass


class DataProtectionError(ProtectedStoreError):
    pass


class ProtectedStoreKind(Enum):
    ACCOUNT = "account"
    FAVORITES = "favorites"


class DataProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class CurrentUserDpapi:
    def __init__(self):
        if os.name != "nt":
            raise DataProtectionError("当前系统不支持 Windows DPAPI")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._configure_functions()
        except (AttributeError, OSError, TypeError):
            raise DataProtectionError("Windows DPAPI 初始化失败") from None

    def _configure_functions(self) -> None:
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    @staticmethod
    def _input_blob(data: bytes) -> tuple[ctypes.Array, _DataBlob]:
        if not isinstance(data, bytes):
            raise DataProtectionError("受保护数据类型无效")
        buffer = ctypes.create_string_buffer(data or b"\0")
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return buffer, blob

    def _free(self, pointer) -> None:
        if pointer:
            self._kernel32.LocalFree(
                ctypes.cast(pointer, wintypes.HLOCAL)
            )

    def _take_output(self, blob: _DataBlob) -> bytes:
        try:
            if blob.cbData and not blob.pbData:
                raise DataProtectionError("Windows DPAPI 返回无效数据")
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            self._free(blob.pbData)

    def protect(self, plaintext: bytes) -> bytes:
        input_buffer, input_blob = self._input_blob(plaintext)
        output_blob = _DataBlob()
        ctypes.set_last_error(0)
        result = self._crypt32.CryptProtectData(
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
            self._free(output_blob.pbData)
            raise DataProtectionError("Windows 无法加密本地数据") from None
        return self._take_output(output_blob)

    def unprotect(self, ciphertext: bytes) -> bytes:
        input_buffer, input_blob = self._input_blob(ciphertext)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        ctypes.set_last_error(0)
        result = self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        self._free(description)
        if not result:
            self._free(output_blob.pbData)
            raise DataProtectionError("Windows 无法解密本地数据") from None
        return self._take_output(output_blob)


@dataclass(frozen=True, slots=True)
class _StoreSpec:
    kind: ProtectedStoreKind
    filename: str
    payload_schema_version: int
    max_plaintext_bytes: int

    @property
    def max_ciphertext_bytes(self) -> int:
        return self.max_plaintext_bytes + DPAPI_MAX_OVERHEAD_BYTES

    @property
    def max_file_bytes(self) -> int:
        encoded = ((self.max_ciphertext_bytes + 2) // 3) * 4
        return encoded + 2048


_STORE_SPECS = {
    ProtectedStoreKind.ACCOUNT: _StoreSpec(
        ProtectedStoreKind.ACCOUNT,
        "account.dat",
        1,
        ACCOUNT_MAX_PLAINTEXT_BYTES,
    ),
    ProtectedStoreKind.FAVORITES: _StoreSpec(
        ProtectedStoreKind.FAVORITES,
        "favorites.dat",
        1,
        FAVORITES_MAX_PLAINTEXT_BYTES,
    ),
}


class ProtectedStore:
    def __init__(
        self,
        kind: ProtectedStoreKind,
        paths: AppPaths = DEFAULT_PATHS,
        protector: DataProtector | None = None,
    ):
        if not isinstance(kind, ProtectedStoreKind):
            raise TypeError("kind must be ProtectedStoreKind")
        if not isinstance(paths, AppPaths):
            raise TypeError("paths must be AppPaths")
        if protector is not None and (
            not callable(getattr(protector, "protect", None))
            or not callable(getattr(protector, "unprotect", None))
        ):
            raise TypeError("protector must implement protect and unprotect")
        self.paths = paths
        self.spec = _STORE_SPECS[kind]
        self._protector = protector or CurrentUserDpapi()

    @classmethod
    def account(
        cls,
        paths: AppPaths = DEFAULT_PATHS,
        protector: DataProtector | None = None,
    ) -> "ProtectedStore":
        return cls(ProtectedStoreKind.ACCOUNT, paths, protector)

    @classmethod
    def favorites(
        cls,
        paths: AppPaths = DEFAULT_PATHS,
        protector: DataProtector | None = None,
    ) -> "ProtectedStore":
        return cls(ProtectedStoreKind.FAVORITES, paths, protector)

    @property
    def kind(self) -> ProtectedStoreKind:
        return self.spec.kind

    @property
    def path(self) -> Path:
        if self.kind is ProtectedStoreKind.ACCOUNT:
            return self.paths.account_file
        return self.paths.favorites_file

    @property
    def max_plaintext_bytes(self) -> int:
        return self.spec.max_plaintext_bytes

    @property
    def payload_schema_version(self) -> int:
        return self.spec.payload_schema_version

    @property
    def max_file_bytes(self) -> int:
        return self.spec.max_file_bytes

    def load(self) -> dict | None:
        self.cleanup_stale_temporaries()
        target = self.path
        self._validate_target(target, allow_missing=True)
        if not target.exists():
            return None
        raw = self._read_regular_file(target, self.max_file_bytes)
        try:
            ciphertext = self._decode_envelope(raw)
            plaintext = self._protector.unprotect(ciphertext)
            return self._decode_payload(plaintext)
        except (
            UnsupportedProtectedStoreVersion,
            UnsupportedProtectedPayloadVersion,
        ):
            raise
        except ProtectedStorePathError:
            raise
        except Exception:
            raise ProtectedStoreUnreadableError(
                f"{self.spec.filename} 无法读取"
            ) from None

    def save(self, payload: Mapping) -> None:
        self.cleanup_stale_temporaries()
        self._validate_target(self.path, allow_missing=True)
        plaintext = self._encode_payload(payload)
        try:
            ciphertext = self._protector.protect(plaintext)
        except Exception:
            raise ProtectedStoreWriteError(
                f"{self.spec.filename} 无法加密"
            ) from None
        if (
            not isinstance(ciphertext, bytes)
            or not ciphertext
            or len(ciphertext) > self.spec.max_ciphertext_bytes
        ):
            raise ProtectedStoreWriteError(
                f"{self.spec.filename} 加密结果无效"
            )
        envelope = self._encode_envelope(ciphertext)
        self._write_atomic(self.path, envelope)

    def delete(self) -> None:
        self._validate_target(self.path, allow_missing=True)
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ProtectedStoreDeleteError(
                f"无法删除 {self.spec.filename}"
            ) from None
        self.cleanup_stale_temporaries()

    def cleanup_stale_temporaries(self) -> None:
        root = self._validated_root()
        pattern = f".{self.spec.filename}.*.tmp"
        try:
            candidates = tuple(self.paths.root.glob(pattern))
        except OSError:
            return
        for candidate in candidates:
            try:
                state = candidate.lstat()
                if not stat.S_ISREG(state.st_mode) or _is_reparse_state(state):
                    continue
                if candidate.resolve(strict=True).parent != root:
                    continue
                candidate.unlink()
            except (FileNotFoundError, OSError):
                continue

    def _validated_root(self) -> Path:
        root = self.paths.root
        try:
            state = root.lstat()
            if not stat.S_ISDIR(state.st_mode) or _is_reparse_state(state):
                raise ProtectedStorePathError("程序目录不是普通目录")
            resolved = root.resolve(strict=True)
        except ProtectedStorePathError:
            raise
        except OSError:
            raise ProtectedStorePathError("程序目录无法访问") from None
        return resolved

    def _validate_target(self, target: Path, *, allow_missing: bool) -> None:
        root = self._validated_root()
        expected = self.paths.root / self.spec.filename
        if target != expected:
            raise ProtectedStorePathError("受保护文件路径无效")
        try:
            state = target.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise ProtectedStorePathError("受保护文件不存在") from None
        except OSError:
            raise ProtectedStorePathError("受保护文件无法访问") from None
        if not stat.S_ISREG(state.st_mode) or _is_reparse_state(state):
            raise ProtectedStorePathError("受保护文件不是普通文件")
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            raise ProtectedStorePathError("受保护文件无法解析") from None
        if resolved.parent != root or resolved.name != self.spec.filename:
            raise ProtectedStorePathError("受保护文件超出程序目录")

    def _read_regular_file(self, target: Path, limit: int) -> bytes:
        descriptor = None
        try:
            before = target.lstat()
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            after = os.fstat(descriptor)
            if not stat.S_ISREG(after.st_mode) or _is_reparse_state(after):
                raise ProtectedStorePathError("受保护文件不是普通文件")
            if not _same_file_state(before, after):
                raise ProtectedStorePathError("受保护文件在读取前发生变化")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw = handle.read(limit + 1)
        except ProtectedStorePathError:
            raise
        except OSError:
            raise ProtectedStoreUnreadableError(
                f"{self.spec.filename} 无法读取"
            ) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if len(raw) > limit:
            raise ProtectedStoreUnreadableError(
                f"{self.spec.filename} 文件过大"
            )
        return raw

    def _encode_payload(self, payload: Mapping) -> bytes:
        if not isinstance(payload, Mapping):
            raise ProtectedStoreValidationError("受保护载荷必须是对象")
        version = payload.get("schema_version")
        if type(version) is not int:
            raise ProtectedStoreValidationError("受保护载荷版本无效")
        if version > self.payload_schema_version:
            raise UnsupportedProtectedPayloadVersion(
                "受保护载荷版本高于程序支持的版本"
            )
        if version != self.payload_schema_version:
            raise ProtectedStoreValidationError("不支持的受保护载荷版本")
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except Exception:
            raise ProtectedStoreValidationError("受保护载荷无法编码") from None
        if not encoded or len(encoded) > self.max_plaintext_bytes:
            raise ProtectedStoreValidationError("受保护载荷大小无效")
        return encoded

    def _decode_payload(self, plaintext: bytes) -> dict:
        if (
            not isinstance(plaintext, bytes)
            or not plaintext
            or len(plaintext) > self.max_plaintext_bytes
        ):
            raise ProtectedStoreValidationError("受保护载荷大小无效")
        try:
            payload = json.loads(
                plaintext.decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise ProtectedStoreValidationError("受保护载荷无法解析") from None
        if not isinstance(payload, dict):
            raise ProtectedStoreValidationError("受保护载荷必须是对象")
        version = payload.get("schema_version")
        if type(version) is not int:
            raise ProtectedStoreValidationError("受保护载荷版本无效")
        if version > self.payload_schema_version:
            raise UnsupportedProtectedPayloadVersion(
                "受保护载荷版本高于程序支持的版本"
            )
        if version != self.payload_schema_version:
            raise ProtectedStoreValidationError("不支持的受保护载荷版本")
        return payload

    def _encode_envelope(self, ciphertext: bytes) -> bytes:
        envelope = {
            "format": ENVELOPE_FORMAT,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "kind": self.kind.value,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return (
            json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("ascii")

    def _decode_envelope(self, raw: bytes) -> bytes:
        try:
            envelope = json.loads(
                raw.decode("ascii"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            raise ProtectedStoreValidationError("受保护文件外壳无效") from None
        if not isinstance(envelope, dict):
            raise ProtectedStoreValidationError("受保护文件外壳无效")
        if envelope.get("format") != ENVELOPE_FORMAT:
            raise ProtectedStoreValidationError("受保护文件格式无效")
        version = envelope.get("schema_version")
        if type(version) is not int:
            raise ProtectedStoreValidationError("受保护文件版本无效")
        if version > ENVELOPE_SCHEMA_VERSION:
            raise UnsupportedProtectedStoreVersion(
                "受保护文件版本高于程序支持的版本"
            )
        if version != ENVELOPE_SCHEMA_VERSION:
            raise ProtectedStoreValidationError("不支持的受保护文件版本")
        if envelope.get("kind") != self.kind.value:
            raise ProtectedStoreValidationError("受保护文件类型不匹配")
        if set(envelope) != {
            "format",
            "schema_version",
            "kind",
            "ciphertext",
        }:
            raise ProtectedStoreValidationError("受保护文件字段无效")
        encoded = envelope.get("ciphertext")
        if not isinstance(encoded, str) or not encoded or not encoded.isascii():
            raise ProtectedStoreValidationError("受保护文件密文无效")
        max_encoded = ((self.spec.max_ciphertext_bytes + 2) // 3) * 4
        if len(encoded) > max_encoded:
            raise ProtectedStoreValidationError("受保护文件密文过大")
        try:
            ciphertext = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ProtectedStoreValidationError("受保护文件密文无效") from None
        if not ciphertext or len(ciphertext) > self.spec.max_ciphertext_bytes:
            raise ProtectedStoreValidationError("受保护文件密文大小无效")
        return ciphertext

    def _write_atomic(self, target: Path, payload: bytes) -> None:
        temporary = None
        descriptor = None
        root_before = self._validated_root()
        try:
            descriptor, name = tempfile.mkstemp(
                dir=self.paths.root,
                prefix=f".{self.spec.filename}.",
                suffix=".tmp",
            )
            temporary = Path(name)
            state = temporary.lstat()
            if not stat.S_ISREG(state.st_mode) or _is_reparse_state(state):
                raise ProtectedStorePathError("受保护临时文件不是普通文件")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            root_after = self._validated_root()
            if root_after != root_before:
                raise ProtectedStorePathError("程序目录在保存期间发生变化")
            if temporary.resolve(strict=True).parent != root_after:
                raise ProtectedStorePathError("受保护临时文件超出程序目录")
            self._validate_target(target, allow_missing=True)
            os.replace(temporary, target)
            temporary = None
        except ProtectedStorePathError:
            raise
        except OSError:
            raise ProtectedStoreWriteError(
                f"无法保存 {self.spec.filename}"
            ) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _is_reparse_state(state) -> bool:
    attributes = getattr(state, "st_file_attributes", 0)
    return stat.S_ISLNK(state.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _same_file_state(first, second) -> bool:
    if first.st_dev != second.st_dev:
        return False
    first_inode = getattr(first, "st_ino", 0)
    second_inode = getattr(second, "st_ino", 0)
    return not first_inode or not second_inode or first_inode == second_inode


__all__ = [
    "ACCOUNT_MAX_PLAINTEXT_BYTES",
    "CRYPTPROTECT_UI_FORBIDDEN",
    "CurrentUserDpapi",
    "DataProtectionError",
    "FAVORITES_MAX_PLAINTEXT_BYTES",
    "ProtectedStore",
    "ProtectedStoreDeleteError",
    "ProtectedStoreError",
    "ProtectedStoreKind",
    "ProtectedStorePathError",
    "ProtectedStoreUnreadableError",
    "ProtectedStoreValidationError",
    "ProtectedStoreWriteError",
    "UnsupportedProtectedPayloadVersion",
    "UnsupportedProtectedStoreVersion",
]
