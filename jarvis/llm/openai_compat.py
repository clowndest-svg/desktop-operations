"""OpenAI-compatible chat client (GPT / Claude / Kimi / DeepSeek / Qwen / ...).

One implementation covers every provider that speaks the de-facto standard
``POST {base_url}/chat/completions`` protocol. Provider differences live in
configuration (base URL, model, API key env var, prices) — never in code.

Security: the API key is **never** stored in configuration. Config carries
only the *name* of an environment variable (``api_key_env``); the value is
read at request time so key rotation needs no restart, and phase 14 can swap
the source for an encrypted store behind the same lookup.

Accounting: every request logs ``latency_ms/tokens_in/tokens_out/cost_usd``
through :func:`jarvis.logging.metrics` (spec chapter 15).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from jarvis.core.events import UsageEvent
from jarvis.llm.errors import (
    LlmAuthError,
    LlmConnectionError,
    LlmContextOverflowError,
    LlmError,
    LlmPermissionError,
    LlmRateLimitError,
    LlmRequestError,
    LlmResponseError,
    LlmServerError,
    LlmTimeoutError,
)
from jarvis.llm.transport import (
    HttpTransport,
    TransportNetworkError,
    TransportStatusError,
    TransportTimeoutError,
    UrllibTransport,
)
from jarvis.llm.types import (
    ChatMessage,
    ChatResponse,
    GenerationOptions,
    StreamChunk,
    StreamToolCall,
    ToolCall,
    Usage,
)
from jarvis.logging import metrics

__all__ = ["OpenAiCompatClient", "OpenAiCompatSettings"]

logger = logging.getLogger("jarvis.llm.client")

_CHAT_COMPLETIONS_PATH = "/chat/completions"
_STREAM_DONE_SENTINEL = "[DONE]"
_RETRYABLE_ERRORS = (LlmRateLimitError, LlmServerError, LlmTimeoutError, LlmConnectionError)
_MEDIA_HANDOFF_SECONDS = 20.0
"""How long a bundled chat request waits for the media engine before saying why it can't."""

"""What each vendor says when the prompt does not fit (all measured or quoted from their
own error bodies): llama.cpp's server, Ollama, vLLM, OpenAI and Anthropic all phrase it
differently, and every one of them is a 400 that looks like "bad request" to the panel."""
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "exceed_context_size_error",
    "exceeds the available context size",
    "context length exceeded",
    "maximum context length",
    "exceeded max context length",
    "exceeds the context window",
    "exceeds the model's context",
    "token context window",
    "prompt is too long",
)

"""A rejection that is not about the *shape* of the request. These must not be counted as
"this provider cannot stream" -- that flag is sticky, and one unrelated 400 would leave
every later answer arriving in one block with no way to interrupt it."""
_NOT_A_STREAMING_PROBLEM: frozenset[str] = frozenset(
    {"overflow", "auth", "forbidden", "quota", "timeout", "network"}
)


_LOOPBACK = re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?", re.I)


def _local_endpoint_hint(base_url: str) -> str:
    """连的是本机却没连上：把端口写进报错，并说清她自带那个模型在哪个端口。

    只在本机地址上说这些 —— 云端那一家连不上，扯自带模型只会把人带偏。
    自带模型那一行读的是它自己那份状态，**不会**因为这次报错去起进程。
    """
    mine = _LOOPBACK.match(base_url or "")
    if not mine:
        return ""
    asked = mine.group(1) or "80"
    try:
        from jarvis.llm import local_server

        running = local_server.shared_server().endpoint() or ""
    except Exception:
        running = ""
    hit = _LOOPBACK.match(running)
    theirs = (hit.group(1) or "80") if hit else ""
    if theirs and theirs != asked:
        return (
            f"（这一行连的是 127.0.0.1:{asked}，她自带那个模型在 127.0.0.1:{theirs} 上在跑 —— "
            "两个不是同一个地址；要用自带那个，把上面那行模型换成它）"
        )
    if theirs == asked:
        return (
            f"（这一行连的就是她自带那个模型的端口 127.0.0.1:{asked}，可它连不上 —— "
            "那个进程大概已经不在了：设置 · 自带模型 点「停掉」再点「现在加载」）"
        )
    return f"（这一行连的是 127.0.0.1:{asked}，本机现在没有东西在听这个端口）"


@dataclass(frozen=True, slots=True)
class _StreamOutcome:
    """What one attempt at a streamed response produced.

    ``response`` is ``None`` only when the endpoint refused the request before any chunk
    arrived, which is the single case where trying a different streaming shape cannot
    duplicate an answer the operator is already watching.
    """

    response: ChatResponse | None
    error: str = ""
    kind: str = ""
    """The rejection's taxonomy token, kept alongside the text so the caller can tell
    "this endpoint cannot stream" from "this prompt does not fit" -- both arrive as a 400
    before the first chunk, and only one of them is about the streaming shape."""


@dataclass(frozen=True, slots=True)
class OpenAiCompatSettings:
    """Everything the client needs, decoupled from the config schema."""

    provider_name: str
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    cost_input_per_1m: float = 0.0
    """USD per 1M prompt tokens (0 = unknown, cost not reported)."""

    cost_output_per_1m: float = 0.0
    """USD per 1M completion tokens (0 = unknown, cost not reported)."""

    key_optional: bool = False
    """Send the request without an ``Authorization`` header when no key is configured."""
    bundled: bool = False
    managed_resources: bool = False
    """这一发是发给"她自己带的那个离线模型"的：请求在飞时不许被空闲看门狗关掉。"""


class OpenAiCompatClient:
    """Synchronous :class:`~jarvis.llm.client.LlmClient` implementation.

    Args:
        settings: Provider endpoint, model and retry/pricing policy.
        transport: HTTP transport; defaults to the stdlib implementation.
        sleep: Injectable delay function (tests pass a recorder).
        environ: Injectable environment mapping (tests avoid ``os.environ``).
        on_usage: Called once per answered request with its token accounting. The
            ``llm`` layer must not know about a database, so whoever wants the
            numbers kept hands in a sink -- see :class:`UsageEvent`.
    """

    def __init__(
        self,
        settings: OpenAiCompatSettings,
        transport: HttpTransport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        environ: Mapping[str, str] | None = None,
        on_usage: Callable[[UsageEvent], None] | None = None,
    ) -> None:
        self._settings = settings
        self._transport: HttpTransport = transport if transport is not None else UrllibTransport()
        self._sleep = sleep
        self._environ = environ
        self._on_usage = on_usage
        self._endpoint = settings.base_url.rstrip("/") + _CHAT_COMPLETIONS_PATH
        self._stream_rejected = False
        """Set once an endpoint refuses a streaming request outright.

        Remembered on the client rather than re-discovered per turn, because the second
        discovery would be another failed request in front of the user. Cleared never:
        this is a property of the endpoint, and one that changes is a config change.
        """
        self._include_usage = True
        """Whether to ask for the trailing usage block. Dropped if the endpoint objects.

        Two settings and not one because they fail separately: an endpoint can accept
        streaming but reject ``stream_options``, and losing streaming entirely to keep a
        token count nobody reads would be the wrong trade.
        """

    # -- LlmClient protocol -------------------------------------------------

    @property
    def provider_name(self) -> str:
        return self._settings.provider_name

    @property
    def model(self) -> str:
        return self._settings.model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> ChatResponse:
        payload = self._payload(messages, options, stream=False)
        started = time.perf_counter()
        raw = self._post_with_retry(payload)
        response = self._parse_response(raw)
        self._log_done("complete ok", started, response.usage, _task_of(options))
        return response

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Iterator[StreamChunk]:
        payload = self._payload(messages, options, stream=True)
        return self._stream_chunks(payload, _task_of(options))

    def _stream_chunks(
        self, payload: Mapping[str, object], task_id: str = ""
    ) -> Generator[StreamChunk, None, None]:
        """Iterate an already-built streaming payload.

        ``task_id`` travels alongside rather than being read back out of the payload: the
        payload is what goes on the wire and must not carry a field no provider was asked
        about, and reading it off ambient state would be shared by every concurrent turn.
        """
        started = time.perf_counter()
        with self._held_for_request():
            if self._settings.managed_resources:
                from jarvis.llm import local_server

                payload = dict(payload, model=local_server.selected_card().alias)
            yield from self._iter_chunks(payload, task_id, started)

    def _iter_chunks(
        self, payload: Mapping[str, object], task_id: str, started: float
    ) -> Generator[StreamChunk, None, None]:
        """真正读流那一段。空闲记账在外层那个生成器里，别在这里重复记。"""
        lines = self._open_stream_with_retry(payload)
        usage: Usage | None = None
        try:
            for data in lines:
                if data == _STREAM_DONE_SENTINEL:
                    break
                chunk = self._parse_chunk(data)
                if chunk.usage is not None:
                    usage = chunk.usage
                # Reasoning, tool fragments and the usage-only final chunk are yielded too,
                # not just text: a caller assembling a reply cannot tell "the model thought
                # for 800 tokens" from "the provider never said" unless the chunk that says
                # it reaches it, and those numbers are what the 用量 panel is built from.
                if (
                    chunk.text
                    or chunk.reasoning
                    or chunk.tool_calls
                    or chunk.usage is not None
                    or chunk.finish_reason is not None
                ):
                    yield chunk
        except GeneratorExit:
            # Logged here rather than skipped: the operator stopped this answer, but the
            # request happened and it cost something. Without the line the turn leaves no
            # trace at all, and "cancelled" is indistinguishable from "never asked".
            # ``usage`` is whatever had been reported by then -- usually nothing, since
            # providers send it last, so the ledger shows 无读数 instead of a zero.
            self._log_done("stream abandoned", started, usage, task_id)
            raise
        self._log_done("stream ok", started, usage, task_id)

    def complete_stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
        on_text: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ChatResponse:
        """Ask for an answer and report its text while it arrives.

        This exists so a turn can be interrupted and watched. ``complete`` hands back
        one opaque block after twenty seconds; here the caller learns what the model
        said as it said it, and a stop request lands within a chunk instead of after
        the whole response -- which is what makes 停止 mean now.

        Tool calls are assembled from their fragments before this returns, so the
        caller gets the same :class:`ChatResponse` shape it would get from ``complete``,
        streamed or not.
        """
        if self._stream_rejected:
            return self.complete(messages, options=options)
        include_usage = self._include_usage
        while True:
            payload = self._payload(messages, options, stream=True, include_usage=include_usage)
            result = self._collect_stream(
                payload,
                task_id=_task_of(options),
                on_text=on_text,
                should_stop=should_stop,
            )
            if result.response is not None:
                return result.response
            # The endpoint rejected the request before anything arrived. There are two
            # ways that happens and they need different answers, so both are tried --
            # each only once, and remembered, because re-probing per turn would be a
            # failed request in front of every answer.
            if result.kind in _NOT_A_STREAMING_PROBLEM:
                # Measured: a local server answered this same 400 for a prompt that did
                # not fit its 4096-token window, and the old code concluded "this provider
                # does not stream" -- a sticky flag that then cost every later answer its
                # 停止 button. The request is what was wrong here, so say that instead.
                return self.complete(messages, options=options)
            if include_usage:
                include_usage = False
                self._include_usage = False
                logger.warning(
                    "provider %s rejected the streaming request with the usage block asked "
                    "for; streaming without it from now on",
                    self.provider_name,
                )
                continue
            self._stream_rejected = True
            logger.warning(
                "provider %s does not take streaming requests (%s); answering without it",
                self.provider_name,
                result.error,
            )
            return self.complete(messages, options=options)

    def _collect_stream(
        self,
        payload: Mapping[str, object],
        *,
        task_id: str,
        on_text: Callable[[str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> _StreamOutcome:
        """Fold one streaming response into a :class:`ChatResponse`.

        Returns an empty outcome when the request was refused before any chunk arrived,
        which is the only state in which re-asking is safe -- after half an answer,
        replaying it would put two halves on the screen.
        """
        stop = should_stop if should_stop is not None else _never
        parts: list[str] = []
        thoughts: list[str] = []
        slots: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        model = self._settings.model
        finish: str | None = None
        cancelled = False
        pump = _ChunkPump(self._stream_chunks(payload, task_id))
        try:
            while True:
                kind, item = pump.take(_STOP_POLL_SECONDS)
                if kind == "pending":
                    if stop():
                        cancelled = True
                        break
                    continue
                if kind == "done":
                    break
                if kind == "error":
                    if isinstance(item, BaseException):
                        raise item
                    raise LlmError("流式响应异常中断")
                if kind != "chunk" or not isinstance(item, StreamChunk):
                    break
                chunk = item
                if chunk.model:
                    model = chunk.model
                if chunk.usage is not None:
                    usage = chunk.usage
                if chunk.finish_reason is not None:
                    finish = chunk.finish_reason
                if chunk.reasoning:
                    thoughts.append(chunk.reasoning)
                if chunk.text:
                    parts.append(chunk.text)
                    if on_text is not None:
                        # The whole partial, not the increment: whoever renders this can
                        # lose a delivery and still be correct on the next one.
                        on_text("".join(parts))
                for fragment in chunk.tool_calls:
                    _collect_tool_fragment(slots, fragment)
                if stop():
                    cancelled = True
                    break
        except LlmError as exc:
            if parts or thoughts or slots:
                raise
            return _StreamOutcome(None, str(exc), exc.kind)
        finally:
            # Closed rather than left to the collector: abandoning the loop is how a
            # cancelled turn stops reading the socket.
            pump.close()
        if cancelled:
            finish = "cancelled"
        return _StreamOutcome(
            ChatResponse(
                content="".join(parts),
                model=model,
                finish_reason=finish,
                usage=usage,
                tool_calls=_assemble_tool_calls(slots),
                # 和上面 `content` 一样是**增量流**，拼回去当然也不许加分隔符：
                # 这里以前用 "\n\n" 串起来，于是屏上"思考过程"每行只有一两个字
                # （一个 token 一行），而回答那一段是正常的 —— 同一次请求两种排版。
                reasoning="".join(thoughts),
            ),
            "",
        )

    # -- request building ----------------------------------------------------

    def _api_key(self) -> str:
        env = os.environ if self._environ is None else self._environ
        key = env.get(self._settings.api_key_env, "").strip()
        if not key and self._settings.key_optional:
            # Nothing to send, and that is the honest answer: Ollama, LM Studio and
            # llama.cpp's server all speak the OpenAI protocol with no credential.
            # Returning "" makes :meth:`_headers` leave the Authorization line off --
            # sending an empty ``Bearer `` instead is a different request, and some
            # proxies answer it with a 401 that reads exactly like a broken client.
            return ""
        if not key:
            raise LlmAuthError(
                f"API key environment variable '{self._settings.api_key_env}' is not set",
                details={
                    "provider": self._settings.provider_name,
                    "variable": self._settings.api_key_env,
                },
            )
        return key

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        options: GenerationOptions | None,
        *,
        stream: bool,
        include_usage: bool = True,
    ) -> dict[str, object]:
        if not messages:
            raise LlmRequestError("cannot send an empty message list")
        payload: dict[str, object] = {
            "model": self._settings.model,
            "messages": [_serialize_message(message) for message in messages],
        }
        if stream:
            payload["stream"] = True
            if include_usage:
                # Ask for the usage block at the end of the stream. Without it an
                # OpenAI-compatible endpoint answers in full but reports no tokens, so
                # every 用量 number for a streamed turn would go missing -- which is
                # exactly how making streaming the default turns into a silent
                # regression downstream. An endpoint that rejects this parameter is
                # handled by :meth:`complete_stream`, which drops it and remembers.
                payload["stream_options"] = {"include_usage": True}
        if options is not None:
            if options.temperature is not None:
                payload["temperature"] = options.temperature
            if options.max_tokens is not None:
                payload["max_tokens"] = options.max_tokens
            if options.top_p is not None:
                payload["top_p"] = options.top_p
            if options.thinking is not None:
                payload["enable_thinking"] = options.thinking
            if options.thinking_budget is not None:
                # Sent on its own merits: a budget with thinking off is a provider's
                # problem, not ours, and the alternative is a knob that can only be
                # set together with another one.
                payload["thinking_budget"] = options.thinking_budget
            if options.template_kwargs:
                # llama.cpp's own switch for Qwen3-style hybrid thinking. Sent only when a
                # caller put something here, which today means the bundled server.
                payload["chat_template_kwargs"] = dict(options.template_kwargs)
            if options.tools:
                payload["tools"] = [dict(tool) for tool in options.tools]
                payload["tool_choice"] = options.tool_choice or "auto"
        return payload

    # -- retry policy ----------------------------------------------------------

    @contextmanager
    def _held_for_request(self) -> Iterator[None]:
        """自带模型那一行：这一发没打完之前，空闲看门狗不许把她关掉。

        实测（用户机器，r83）：Qwen3-4B 在 8K + 工具表那一句答了 120 秒还没完，看门狗那份
        从"加载完成"起算的表到点就把进程收了 —— 屏幕上只剩 WinError 10054。
        """
        if not self._settings.bundled:
            yield
            return
        from jarvis.llm import local_server

        server = local_server.shared_server()
        if self._settings.managed_resources:
            # 生图/生视频那一段整件持有资源锁（media_session）。无等待地排队会让
            # 「思考中」挂到那件做完为止，而停止按钮掐不动一次锁等待 —— 所以这里
            # 等一小会儿就明说原因，别把一次可解释的拒绝换成一次看不见的等待。
            if not local_server.resource_lock().acquire(timeout=_MEDIA_HANDOFF_SECONDS):
                raise LlmConnectionError(
                    "本地图片或视频正在用这张显卡，聊天模型暂时让给了它。"
                    "等那一件完成再问，或先在弹窗里点停止。"
                )
            try:
                ready, reason = server.ensure_running()
                if not ready:
                    raise LlmConnectionError(reason or "本地模型未能启动")
                self._endpoint = local_server.endpoint().rstrip("/") + _CHAT_COMPLETIONS_PATH
                server.begin_request()
                try:
                    yield
                finally:
                    server.end_request()
            finally:
                local_server.resource_lock().release()
            return
        server.begin_request()
        try:
            yield
        finally:
            server.end_request()

    def _post_with_retry(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        with self._held_for_request():
            if self._settings.managed_resources:
                from jarvis.llm import local_server

                payload = dict(payload, model=local_server.selected_card().alias)
            return self._post_once(payload)

    def _post_once(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        attempt = 0
        while True:
            try:
                return self._transport.post_json(
                    self._endpoint,
                    headers=self._headers(),
                    payload=payload,
                    timeout=self._settings.timeout_seconds,
                )
            except (TransportStatusError, TransportTimeoutError, TransportNetworkError) as exc:
                error = self._map_transport_error(exc)
                attempt += 1
                if not self._should_retry(error, attempt):
                    raise error from exc
                self._backoff(attempt, error)

    def _open_stream_with_retry(self, payload: Mapping[str, object]) -> Iterator[str]:
        """Open the SSE stream, retrying only until the first line arrives."""
        attempt = 0
        while True:
            try:
                # A transport may fail while constructing its iterator or on its
                # first next(). Both are pre-stream failures and share retry policy.
                iterator = self._transport.post_sse(
                    self._endpoint,
                    headers=self._headers(),
                    payload=payload,
                    timeout=self._settings.timeout_seconds,
                )
                first = next(iterator)
            except StopIteration:
                return iter(())
            except (TransportStatusError, TransportTimeoutError, TransportNetworkError) as exc:
                error = self._map_transport_error(exc)
                attempt += 1
                if not self._should_retry(error, attempt):
                    raise error from exc
                self._backoff(attempt, error)
                continue
            return self._guarded_stream(first, iterator)

    def _guarded_stream(self, first: str, lines: Iterator[str]) -> Iterator[str]:
        """Map transport failures raised *mid-stream* (no retry possible).

        The transport iterator is closed on the way out instead of being left to
        refcounting. This is the path a 停止 request takes, and the point of stopping is
        that the request stops: "the socket closed when the last reference happened to
        drop" is a CPython implementation detail, not something to hang a cancel button on.
        """
        try:
            yield first
            yield from lines
        except (TransportStatusError, TransportTimeoutError, TransportNetworkError) as exc:
            raise self._map_transport_error(exc) from exc
        finally:
            close = getattr(lines, "close", None)
            if close is not None:
                close()

    def _should_retry(self, error: LlmError, attempt: int) -> bool:
        return isinstance(error, _RETRYABLE_ERRORS) and attempt <= self._settings.max_retries

    def _backoff(self, attempt: int, error: LlmError) -> None:
        delay = self._settings.retry_backoff_seconds * (2 ** (attempt - 1))
        logger.warning(
            "retrying after %s (provider=%s, attempt %d/%d, backoff %.2fs)",
            type(error).__name__,
            self._settings.provider_name,
            attempt,
            self._settings.max_retries,
            delay,
        )
        if delay > 0:
            self._sleep(delay)

    def _map_transport_error(self, exc: Exception) -> LlmError:
        provider = self._settings.provider_name
        details: dict[str, object] = {"provider": provider, "model": self._settings.model}
        if isinstance(exc, TransportTimeoutError):
            details["timeout_seconds"] = self._settings.timeout_seconds
            return LlmTimeoutError(f"request to '{provider}' timed out", details=details)
        if isinstance(exc, TransportNetworkError):
            return LlmConnectionError(
                f"cannot reach '{provider}': {exc}{_local_endpoint_hint(self._settings.base_url)}",
                details=details,
            )
        if isinstance(exc, TransportStatusError):
            details["status"] = exc.status
            details["body"] = exc.body[:500]
            if exc.status == 401:
                return LlmAuthError(f"'{provider}' rejected the API key", details=details)
            if exc.status == 403:
                # 403 with a working key is almost always "this token may not use this
                # model" (measured: a gateway answers 403 with 该令牌无权访问模型 gpt-6.1-sol),
                # so the wording and the panel's advice must not send them to re-paste it.
                return LlmPermissionError(
                    f"'{provider}' refused this model for the key it was given",
                    details=details,
                )
            if exc.status == 429:
                return LlmRateLimitError(f"'{provider}' rate limit hit", details=details)
            if exc.status >= 500:
                return LlmServerError(f"'{provider}' server error {exc.status}", details=details)
            overflow = _overflow_of(exc.body)
            if overflow is not None:
                asked, holds = overflow
                details["prompt_tokens"] = asked
                details["context_tokens"] = holds
                measured = f" (asked {asked} tokens, it holds {holds})" if asked and holds else ""
                return LlmContextOverflowError(
                    f"'{provider}' has no room left for this prompt{measured}",
                    details=details,
                    prompt_tokens=asked,
                    context_tokens=holds,
                )
            return LlmRequestError(
                f"'{provider}' rejected the request (HTTP {exc.status})",
                details=details,
            )
        return LlmError(str(exc), details=details)  # pragma: no cover - defensive

    # -- response parsing --------------------------------------------------------

    def _parse_response(self, raw: Mapping[str, object]) -> ChatResponse:
        choices = raw.get("choices")
        if not isinstance(choices, Sequence) or not choices or isinstance(choices, str):
            raise self._malformed("missing or empty 'choices'", raw)
        first = choices[0]
        if not isinstance(first, Mapping):
            raise self._malformed("choice is not an object", raw)
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise self._malformed("choice has no 'message' object", raw)
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        finish_reason = first.get("finish_reason")
        model = raw.get("model")
        return ChatResponse(
            content=content if isinstance(content, str) else "",
            model=model if isinstance(model, str) else self._settings.model,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            usage=_parse_usage(raw.get("usage")),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            reasoning=reasoning if isinstance(reasoning, str) else "",
        )

    def _parse_chunk(self, data: str) -> StreamChunk:
        try:
            parsed: object = json.loads(data)
        except json.JSONDecodeError as exc:
            raise self._malformed(f"stream chunk is not JSON: {data[:200]}", {}) from exc
        if not isinstance(parsed, Mapping):
            raise self._malformed("stream chunk is not an object", {})
        text = ""
        reasoning = ""
        finish_reason: str | None = None
        fragments: list[StreamToolCall] = []
        choices = parsed.get("choices")
        if isinstance(choices, Sequence) and choices and not isinstance(choices, str):
            first = choices[0]
            if isinstance(first, Mapping):
                delta = first.get("delta")
                if isinstance(delta, Mapping):
                    content = delta.get("content")
                    if isinstance(content, str):
                        text = content
                    thought = delta.get("reasoning_content")
                    if isinstance(thought, str):
                        reasoning = thought
                    fragments = _parse_tool_call_deltas(delta.get("tool_calls"))
                reason = first.get("finish_reason")
                if isinstance(reason, str):
                    finish_reason = reason
        named = parsed.get("model")
        return StreamChunk(
            text=text,
            reasoning=reasoning,
            tool_calls=tuple(fragments),
            model=named if isinstance(named, str) else "",
            finish_reason=finish_reason,
            usage=_parse_usage(parsed.get("usage")),
        )

    def _malformed(self, problem: str, raw: Mapping[str, object]) -> LlmResponseError:
        return LlmResponseError(
            f"malformed response from '{self._settings.provider_name}': {problem}",
            details={
                "provider": self._settings.provider_name,
                "keys": sorted(str(key) for key in raw),
            },
        )

    # -- accounting -----------------------------------------------------------

    def _log_done(self, event: str, started: float, usage: Usage | None, task_id: str = "") -> None:
        latency_ms = (time.perf_counter() - started) * 1000.0
        tokens_in = usage.prompt_tokens if usage is not None else None
        tokens_out = usage.completion_tokens if usage is not None else None
        logger.info(
            "%s (provider=%s, model=%s)",
            event,
            self._settings.provider_name,
            self._settings.model,
            extra=metrics(
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=self._cost(usage),
            ),
        )
        self._report_usage(usage, latency_ms, task_id)

    def _report_usage(self, usage: Usage | None, latency_ms: float, task_id: str = "") -> None:
        """Hand the numbers to whoever is keeping them, without ever failing a reply.

        A provider that omitted ``usage`` produces no event at all rather than a row
        of zeros: the statistics screen reads "no reading" from an empty set, which
        is true, instead of "0% cached", which would not be.
        """
        if self._on_usage is None or usage is None:
            return
        try:
            self._on_usage(
                UsageEvent(
                    provider=self._settings.provider_name,
                    model=self._settings.model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cached_tokens=usage.cached_tokens,
                    latency_ms=latency_ms,
                    task_id=task_id,
                )
            )
        except Exception:  # a broken ledger must not lose the user their answer
            logger.exception("usage sink raised; ignoring")

    def _cost(self, usage: Usage | None) -> float | None:
        if usage is None:
            return None
        rate_in = self._settings.cost_input_per_1m
        rate_out = self._settings.cost_output_per_1m
        if rate_in <= 0 and rate_out <= 0:
            return None
        return (usage.prompt_tokens * rate_in + usage.completion_tokens * rate_out) / 1_000_000


def _overflow_of(body: str) -> tuple[int | None, int | None] | None:
    """``(asked, holds)`` when this error body is a context-overflow complaint, else None.

    The two numbers come from the fields the server itself printed (llama.cpp answers
    with ``n_prompt_tokens`` / ``n_ctx``); they are never guessed out of the sentence's
    digits, because the vendors put them in different orders -- llama.cpp says "request
    (5011) exceeds ... (4096)" while OpenAI says "maximum context length is 4096 ...
    you requested 5011". A pair of ``None``s still means overflow: the panel's advice
    keys off the kind, and the server's own wording is shown in the fold.
    """
    if not body or not any(marker in body.lower() for marker in _OVERFLOW_MARKERS):
        return None
    try:
        parsed: object = json.loads(body)
    except (ValueError, TypeError):
        return (None, None)
    row = parsed.get("error") if isinstance(parsed, Mapping) else None
    if not isinstance(row, Mapping):
        row = parsed if isinstance(parsed, Mapping) else {}
    asked = _int_of(row, "n_prompt_tokens", "prompt_tokens")
    holds = _int_of(row, "n_ctx", "context_tokens", "max_context")
    return (asked, holds)


def _int_of(row: Mapping[str, object], *keys: str) -> int | None:
    """The first of ``keys`` that is really an int in this mapping."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _serialize_message(message: ChatMessage) -> dict[str, object]:
    data: dict[str, object] = {
        "role": message.role.value,
        "content": _message_content(message),
    }
    if message.name is not None:
        data["name"] = message.name
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return data


def _message_content(message: ChatMessage) -> object:
    """The ``content`` field for one message: a string, or parts when there are pictures.

    Plain text stays a bare string rather than a one-element array. Providers are
    unanimous about the string and split on the array -- some strict OpenAI-compatible
    endpoints reject a ``content`` array on a message with no image in it, so making the
    shape uniform would cost turns that never had a picture to lose.
    """
    if not message.images:
        return message.content
    parts: list[dict[str, object]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    parts.extend({"type": "image_url", "image_url": {"url": image}} for image in message.images)
    return parts


def _parse_usage(raw: object) -> Usage | None:
    if not isinstance(raw, Mapping):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if isinstance(prompt, bool) or isinstance(completion, bool):
        return None
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=_parse_cached_tokens(raw),
        reasoning_tokens=_parse_reasoning_tokens(raw),
    )


def _parse_reasoning_tokens(usage: Mapping[str, object]) -> int | None:
    """How many completion tokens were spent thinking, or ``None`` for "not said".

    This is the number the thinking-budget setting has to be checkable against: a
    budget of 40 that the provider ignores is a decoration, and the only way to know
    is to read back what it reports. ``None`` and ``0`` stay distinct for the same
    reason ``cached_tokens`` does -- one is silence, the other is an answer.
    """
    details = usage.get("completion_tokens_details")
    if not isinstance(details, Mapping):
        return None
    nested = details.get("reasoning_tokens")
    if isinstance(nested, bool) or not isinstance(nested, int):
        return None
    return max(0, nested)


def _parse_cached_tokens(usage: Mapping[str, object]) -> int | None:
    """How many prompt tokens came from the provider's cache, or ``None``.

    Two wire shapes matter in practice and they are not compatible: OpenAI-style
    providers nest it as ``prompt_tokens_details.cached_tokens``, while
    Anthropic-style ones put ``cache_read_input_tokens`` at the top level. Reading
    only the first is why this used to look like "the provider never tells us".

    Anything absent stays ``None`` rather than 0 -- the UI distinguishes
    「无读数」 from 「命中 0%」, and a 0 invented here would be indistinguishable
    from a provider that genuinely cached nothing.
    """
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        nested = details.get("cached_tokens")
        if isinstance(nested, int) and not isinstance(nested, bool):
            return max(0, nested)
    flat = usage.get("cache_read_input_tokens")
    if isinstance(flat, int) and not isinstance(flat, bool):
        return max(0, flat)
    return None


def _parse_tool_calls(raw: object) -> tuple[ToolCall, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return ()
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            continue
        call_id = item.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(call_id, str) and isinstance(name, str) and isinstance(arguments, str):
            calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)


def _task_of(options: GenerationOptions | None) -> str:
    """The task a request belongs to, or ``""`` when it was asked outside one.

    Read from the options rather than from anywhere ambient because the options are
    already the per-request carrier, and a task id that had to be set on the client
    would be shared by every concurrent turn -- which is the exact bug this field is
    here to make countable.
    """
    return options.task_id if options is not None else ""


def _string_of(value: object) -> str:
    """A string from a parsed JSON field, or ``""`` when it is not one.

    The wire decides these types, not us, and a provider that sends ``null`` for a
    tool-call name must not turn into a crash in the middle of a streamed answer.
    """
    return value if isinstance(value, str) else ""


class _ChunkPump:
    """Pull the next chunk on a helper thread so a stop can be seen while waiting.

    ``next()`` on a streaming HTTP body blocks for as long as the server likes, and a
    model that is thinking emits *nothing* until it decides -- which is why 停止 used to
    only take effect once another chunk happened to arrive. Waiting on a queue with a
    short timeout instead costs one extra thread per streamed turn and buys a cancel
    that answers in milliseconds.

    The thread is not killed on the way out: Python has no safe way to do that, and the
    abandoned read ends by itself at the request timeout. What *is* guaranteed is that
    the caller stops consuming, and the generator gets closed when it can be.
    """

    def __init__(self, source: Generator[StreamChunk, None, None]) -> None:
        self._source = source
        self._queue: queue.Queue[tuple[str, BaseException | StreamChunk | None]] = queue.Queue(
            maxsize=1
        )
        self._thread = threading.Thread(target=self._run, name="llm-stream-pump", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for chunk in self._source:
                self._queue.put(("chunk", chunk))
        except BaseException as exc:  # relayed to the consumer verbatim, then re-raised
            self._queue.put(("error", exc))
        else:
            self._queue.put(("done", None))
        finally:
            with suppress(Exception):  # may be mid-read in this very thread's generator
                self._source.close()

    def take(self, wait: float) -> tuple[str, BaseException | StreamChunk | None]:
        """``("chunk" | "error" | "done", payload)``, or ``("pending", None)`` on timeout."""
        try:
            return self._queue.get(timeout=wait)
        except queue.Empty:
            return ("pending", None)

    def close(self) -> None:
        """Ask the source to finish; never raise on the way out of a cancelled turn."""
        with suppress(Exception):
            self._source.close()


_STOP_POLL_SECONDS: float = 0.1
"""How long to wait for a chunk before re-checking the stop flag."""


def _never() -> bool:
    """The stop predicate used when nobody can stop this request."""
    return False


def _parse_tool_call_deltas(raw: object) -> list[StreamToolCall]:
    """One chunk's tool-call fragments, kept in the shape they arrived in.

    Deliberately not assembled here: fragments of one call span several chunks and
    several chunks can interleave different calls, so merging is :func:
    `_collect_tool_fragment`'s job, and only the loop that owns the whole response
    knows when it is finished.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    found: list[StreamToolCall] = []
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping):
            function = {}
        index = item.get("index")
        found.append(
            StreamToolCall(
                index=index if isinstance(index, int) else position,
                call_id=_string_of(item.get("id")),
                name=_string_of(function.get("name")),
                arguments=_string_of(function.get("arguments")),
            )
        )
    return found


def _collect_tool_fragment(slots: dict[int, dict[str, str]], fragment: StreamToolCall) -> None:
    """Fold one streamed tool-call fragment into its slot, keyed by ``index``.

    The wire sends ``id`` and ``name`` once and the JSON arguments in pieces, so the
    only correct merge is concatenation per index -- a per-chunk parse would see
    ``{"pa`` and report an unparsable argument list for a call that is perfectly fine.
    """
    slot = slots.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
    if fragment.call_id:
        slot["id"] = fragment.call_id
    if fragment.name:
        slot["name"] = fragment.name
    slot["arguments"] += fragment.arguments


def _assemble_tool_calls(slots: dict[int, dict[str, str]]) -> tuple[ToolCall, ...]:
    """Finish what :func:`_collect_tool_fragment` accumulated, in wire order.

    A fragment-only call with no name is dropped rather than guessed at: half a tool
    call executed is worse than none, and the model will ask again.
    """
    calls: list[ToolCall] = []
    for index in sorted(slots):
        slot = slots[index]
        if slot["name"] and slot["id"]:
            calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=slot["arguments"]))
    return tuple(calls)
