"""Keep a frozen GUI's native libraries out of a separate Python runtime."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Iterator

_dll_lock = threading.Lock()


def clean_external_path(value: str, bundle: str) -> str:
    root = os.path.normcase(os.path.abspath(bundle))
    return os.pathsep.join(
        part
        for part in value.split(os.pathsep)
        if part
        and not os.path.normcase(os.path.abspath(part)).startswith(root + os.sep)
        and os.path.normcase(os.path.abspath(part)) != root
    )


@contextlib.contextmanager
def external_dll_search() -> Iterator[None]:
    """Reset inherited SetDllDirectory only while spawning, then restore the GUI.

    PyInstaller documents this requirement for external programs. In particular,
    the GUI's CPU Torch DLLs must not override the portable CUDA Torch runtime.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        yield
        return
    import ctypes

    with _dll_lock:
        kernel = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(buffer), buffer)
        previous = buffer.value
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        if not kernel.SetDllDirectoryW(None):
            raise OSError("Cannot isolate the external engine's native runtime")
        try:
            yield
        finally:
            kernel.SetDllDirectoryW(previous or None)
