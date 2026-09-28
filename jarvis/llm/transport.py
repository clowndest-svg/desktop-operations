"""HTTP transport abstraction for LLM providers.

:class:`HttpTransport` is the seam between the OpenAI-compatible client and
the network: production uses :class:`UrllibTransport` (stdlib only — no
``httpx``/``requests`` dependency to drag through PyInstaller), tests inject
a fake. The transport is *dumb on purpose*: no retries, no JSON schema
knowledge — it moves bytes and reports failures via the three internal
signal exceptions below, which :mod:`jarvis.llm.openai_compat` maps onto the
public :mod:`jarvis.llm.errors` taxonomy.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Protocol, runtime_checkable

from jarvis.core.constants import DEFAULT_ENCODING

__all__ = [
    "HttpTransport",
    "TransportNetworkError",
    "TransportStatusError",
    "TransportTimeoutError",
    "UrllibTransport",
]

_SSE_DATA_PREFIX = "data:"


class TransportStatusError(Exception):
    """Server answered with a non-2xx HTTP status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class TransportTimeoutError(Exception):
    """The request exceeded the configured timeout."""


class TransportNetworkError(Exception):
    """Connection-level failure (DNS, refused, reset...)."""


@runtime_checkable
class HttpTransport(Protocol):
    """Minimal POST-only HTTP contract needed by chat completions."""

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, object]:
        """POST JSON, return the parsed JSON object body."""
        ...

    def post_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Iterator[str]:
        """POST JSON, yield the payload of each SSE ``data:`` line."""
        ...


def _encode(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode(DEFAULT_ENCODING)


class UrllibTransport:
    """Stdlib implementation of :class:`HttpTransport`."""

    def _request(
        self, url: str, headers: Mapping[str, str], payload: Mapping[str, object]
    ) -> urllib.request.Request:
        merged = {"Content-Type": "application/json", **headers}
        return urllib.request.Request(url, data=_encode(payload), headers=merged, method="POST")

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, object]:
        request = self._request(url, headers, payload)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode(DEFAULT_ENCODING)
        except urllib.error.HTTPError as exc:
            raise TransportStatusError(exc.code, _read_error_body(exc)) from exc
        except TimeoutError as exc:
            raise TransportTimeoutError(str(exc)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TransportTimeoutError(str(exc.reason)) from exc
            raise TransportNetworkError(str(exc.reason)) from exc
        except OSError as exc:  # pragma: no cover - rare socket-level failures
            raise TransportNetworkError(str(exc)) from exc
        parsed: object = json.loads(raw)
        if not isinstance(parsed, Mapping):
            raise TransportStatusError(200, f"expected a JSON object, got: {raw[:200]}")
        return parsed

    def post_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Iterator[str]:
        request = self._request(url, {"Accept": "text/event-stream", **headers}, payload)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise TransportStatusError(exc.code, _read_error_body(exc)) from exc
        except TimeoutError as exc:
            raise TransportTimeoutError(str(exc)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TransportTimeoutError(str(exc.reason)) from exc
            raise TransportNetworkError(str(exc.reason)) from exc
        try:
            for raw_line in response:
                line = raw_line.decode(DEFAULT_ENCODING).strip()
                if line.startswith(_SSE_DATA_PREFIX):
                    yield line.removeprefix(_SSE_DATA_PREFIX).strip()
        except TimeoutError as exc:
            raise TransportTimeoutError(str(exc)) from exc
        except OSError as exc:
            raise TransportNetworkError(str(exc)) from exc
        finally:
            response.close()


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode(DEFAULT_ENCODING, errors="replace")
    except OSError:  # pragma: no cover - body already consumed/closed
        return ""
