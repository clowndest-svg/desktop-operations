"""Current-Windows-user DPAPI vault, kept inside the portable data directory."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from pathlib import Path

_LOCK = threading.RLock()


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is required for this credential store")
    raw = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.windll.crypt32
    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        )
    else:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        )
    if not ok:
        raise OSError("Credential encryption/decryption failed for this Windows user")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(target.data, ctypes.c_void_p))


def read_credentials(root: Path) -> dict[str, str]:
    with _LOCK:
        path = root / "credentials.dpapi.json"
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise OSError("Credential store is invalid")
        return {
            str(key): _crypt(base64.b64decode(value), decrypt=True).decode("utf-8")
            for key, value in payload.items()
            if isinstance(value, str)
        }


def write_credential(root: Path, name: str, value: str | None) -> None:
    with _LOCK:
        try:
            values = read_credentials(root)
        except (OSError, ValueError):
            # Keep the encrypted original when a moved install is used by a new user.
            path = root / "credentials.dpapi.json"
            if path.is_file():
                import time

                path.rename(root / f"credentials.unreadable-{time.time_ns()}.dpapi.json")
            values = {}
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
        root.mkdir(parents=True, exist_ok=True)
        path = root / "credentials.dpapi.json"
        temporary = path.with_suffix(".part")
        payload = {
            key: base64.b64encode(_crypt(text.encode("utf-8"))).decode("ascii")
            for key, text in values.items()
        }
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
