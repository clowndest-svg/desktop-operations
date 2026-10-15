"""Token-scoped generated-video streaming. No arbitrary filesystem URLs."""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path


class MediaFiles:
    def __init__(self) -> None:
        self.validate: Callable[[Path], bool] = lambda _path: False
        self._files: dict[str, Path] = {}
        self._lock = threading.Lock()

    def register(self, path: Path) -> str:
        path = path.resolve()
        if not self.validate(path) or path.suffix.lower() != ".mp4":
            raise OSError("Only generated videos can be streamed")
        with self._lock:
            for key, existing in self._files.items():
                if existing == path:
                    return "media/" + key
            key = secrets.token_urlsafe(32)
            self._files[key] = path
            if len(self._files) > 256:
                self._files.pop(next(iter(self._files)))
        return "media/" + key

    def serve(self, request: BaseHTTPRequestHandler, *, head: bool = False) -> None:
        key = request.path.split("?", 1)[0].removeprefix("/media/")
        with self._lock:
            path = self._files.get(key)
        if path is None or not self.validate(path) or not path.is_file():
            request.send_error(404)
            return
        try:
            with path.open("rb") as source:
                size = path.stat().st_size
                begin, end = 0, size - 1
                ranged = request.headers.get("Range", "")
                if ranged:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", ranged)
                    if not match or not any(match.groups()):
                        request.send_error(416)
                        return
                    if match[1]:
                        begin = int(match[1])
                        end = min(end, int(match[2])) if match[2] else end
                    else:
                        begin = max(0, size - int(match[2]))
                    if begin > end or begin >= size:
                        request.send_error(416)
                        return
                request.send_response(206 if ranged else 200)
                request.send_header("Content-Type", "video/mp4")
                request.send_header("Accept-Ranges", "bytes")
                request.send_header("Content-Length", str(end - begin + 1))
                if ranged:
                    request.send_header("Content-Range", f"bytes {begin}-{end}/{size}")
                request.end_headers()
                if head:
                    return
                source.seek(begin)
                remaining = end - begin + 1
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    request.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, ConnectionError):
            return
