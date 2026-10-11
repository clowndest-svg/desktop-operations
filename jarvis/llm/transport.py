"""HTTP transport abstraction for LLM providers.

:class:`HttpTransport` is the seam between the OpenAI-compatible client and
the network: production uses :class:`UrllibTransport` (stdlib only — no
``httpx``/``requests`` dependency to drag through PyInstaller), tests inject
a fake. The transport is *dumb on purpose*: no retries, no JSON schema
knowledge — it moves bytes and reports failures via the three internal
signal exceptions below, which :mod:`jarvis.llm.openai_compat` maps onto the
public :mod:`jarvis.llm.errors` taxonomy.

One thing it does decide: a URL naming **this** machine is opened without the
system proxy (:func:`open_request`). With ``HTTP_PROXY`` set — which is the normal
state on a machine running clash/v2ray — stdlib would otherwise route
``http://127.0.0.1:11434`` through that proxy, so a local model's health depends on
whether an unrelated program is running, and a stopped proxy turns it into an
``HTTP 502`` that the panel blames on the vendor.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Generator, Iterator, Mapping
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from jarvis.core.constants import DEFAULT_ENCODING, USER_AGENT

__all__ = [
    "HttpTransport",
    "TransportNetworkError",
    "TransportStatusError",
    "TransportTimeoutError",
    "UrllibTransport",
    "is_local_address",
    "open_request",
]

logger = logging.getLogger("jarvis.llm.transport")

_SSE_DATA_PREFIX = "data:"
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "::1"})
_PROXY_VARS: tuple[str, ...] = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")


def is_local_address(url: str) -> bool:
    """Whether this URL names *this* machine.

    Measured, not theoretical: with ``HTTP_PROXY`` in the environment (clash / v2ray /
    公司代理 all set it), stdlib urllib sends ``http://127.0.0.1:11434`` **through the
    proxy**. When the proxy is up that still answers, so nothing looks wrong; when it is
    down, a perfectly healthy local Ollama comes back as ``HTTP 502`` and the panel says
    「厂商自己返回 5xx」 —— 把人支使去查一个根本没问题的厂商。
    """
    host = (urlparse(url).hostname or "").strip("[]").lower().rstrip(".")
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _proxy_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return any(str(env.get(name, "") or "").strip() for name in _PROXY_VARS)


_notice_given = False


def open_request(request: urllib.request.Request, timeout: float) -> Any:
    """Send one request, straight to the machine it names when that machine is this one.

    Only the proxy handling differs from :func:`urllib.request.urlopen`: a
    ``build_opener()`` with the standard handlers is what ``urlopen`` itself uses, so
    redirects, TLS defaults and the error types stay exactly where they were.
    """
    global _notice_given
    if is_local_address(request.full_url):
        if _proxy_configured() and not _notice_given:
            # Once per process, at info: skipping the proxy is deliberate, and this line
            # is the only place 「这次为什么没走代理」 can be answered afterwards.
            _notice_given = True
            logger.info("这个地址是本机的，请求不走系统代理（环境里设了 HTTP_PROXY）")
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=timeout
        )
    return urllib.request.urlopen(request, timeout=timeout)


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
        # The UA default is not decoration: without it stdlib announces itself as
        # ``Python-urllib`` and a Cloudflare-fronted gateway returns 403 "error code: 1010"
        # to a perfectly valid key. A caller that sets its own still wins.
        merged = {"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers}
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
            with open_request(request, timeout) as response:
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
    ) -> Generator[str, None, None]:
        """A generator rather than a bare ``Iterator``, because closing it is the point.

        The protocol above only promises a stream of lines; this implementation is what
        turns "the caller stopped reading" into "the connection dropped", and a caller
        that was handed an ``Iterator`` cannot see the ``close`` that does it.
        """
        request = self._request(url, {"Accept": "text/event-stream", **headers}, payload)
        try:
            response = open_request(request, timeout)
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
