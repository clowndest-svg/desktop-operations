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
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import chain

from jarvis.core.events import UsageEvent
from jarvis.llm.errors import (
    LlmAuthError,
    LlmConnectionError,
    LlmError,
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
    ToolCall,
    Usage,
)
from jarvis.logging import metrics

__all__ = ["OpenAiCompatClient", "OpenAiCompatSettings"]

logger = logging.getLogger("jarvis.llm.client")

_CHAT_COMPLETIONS_PATH = "/chat/completions"
_STREAM_DONE_SENTINEL = "[DONE]"
_RETRYABLE_ERRORS = (LlmRateLimitError, LlmServerError, LlmTimeoutError, LlmConnectionError)


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
        self._log_done("complete ok", started, response.usage)
        return response

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        options: GenerationOptions | None = None,
    ) -> Iterator[StreamChunk]:
        payload = self._payload(messages, options, stream=True)
        started = time.perf_counter()
        lines = self._open_stream_with_retry(payload)
        usage: Usage | None = None
        for data in lines:
            if data == _STREAM_DONE_SENTINEL:
                break
            chunk = self._parse_chunk(data)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.text or chunk.finish_reason is not None:
                yield chunk
        self._log_done("stream ok", started, usage)

    # -- request building ----------------------------------------------------

    def _api_key(self) -> str:
        env = os.environ if self._environ is None else self._environ
        key = env.get(self._settings.api_key_env, "").strip()
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
        return {"Authorization": f"Bearer {self._api_key()}"}

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        options: GenerationOptions | None,
        *,
        stream: bool,
    ) -> dict[str, object]:
        if not messages:
            raise LlmRequestError("cannot send an empty message list")
        payload: dict[str, object] = {
            "model": self._settings.model,
            "messages": [_serialize_message(message) for message in messages],
        }
        if stream:
            payload["stream"] = True
        if options is not None:
            if options.temperature is not None:
                payload["temperature"] = options.temperature
            if options.max_tokens is not None:
                payload["max_tokens"] = options.max_tokens
            if options.top_p is not None:
                payload["top_p"] = options.top_p
            if options.tools:
                payload["tools"] = [dict(tool) for tool in options.tools]
                payload["tool_choice"] = options.tool_choice or "auto"
        return payload

    # -- retry policy ----------------------------------------------------------

    def _post_with_retry(self, payload: Mapping[str, object]) -> Mapping[str, object]:
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
            iterator = self._transport.post_sse(
                self._endpoint,
                headers=self._headers(),
                payload=payload,
                timeout=self._settings.timeout_seconds,
            )
            try:
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
            return self._guarded_stream(chain([first], iterator))

    def _guarded_stream(self, lines: Iterator[str]) -> Iterator[str]:
        """Map transport failures raised *mid-stream* (no retry possible)."""
        try:
            yield from lines
        except (TransportStatusError, TransportTimeoutError, TransportNetworkError) as exc:
            raise self._map_transport_error(exc) from exc

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
                f"cannot reach '{provider}': {exc}",
                details=details,
            )
        if isinstance(exc, TransportStatusError):
            details["status"] = exc.status
            details["body"] = exc.body[:500]
            if exc.status in (401, 403):
                return LlmAuthError(f"'{provider}' rejected the API key", details=details)
            if exc.status == 429:
                return LlmRateLimitError(f"'{provider}' rate limit hit", details=details)
            if exc.status >= 500:
                return LlmServerError(f"'{provider}' server error {exc.status}", details=details)
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
        finish_reason = first.get("finish_reason")
        model = raw.get("model")
        return ChatResponse(
            content=content if isinstance(content, str) else "",
            model=model if isinstance(model, str) else self._settings.model,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            usage=_parse_usage(raw.get("usage")),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
        )

    def _parse_chunk(self, data: str) -> StreamChunk:
        try:
            parsed: object = json.loads(data)
        except json.JSONDecodeError as exc:
            raise self._malformed(f"stream chunk is not JSON: {data[:200]}", {}) from exc
        if not isinstance(parsed, Mapping):
            raise self._malformed("stream chunk is not an object", {})
        text = ""
        finish_reason: str | None = None
        choices = parsed.get("choices")
        if isinstance(choices, Sequence) and choices and not isinstance(choices, str):
            first = choices[0]
            if isinstance(first, Mapping):
                delta = first.get("delta")
                if isinstance(delta, Mapping):
                    content = delta.get("content")
                    if isinstance(content, str):
                        text = content
                reason = first.get("finish_reason")
                if isinstance(reason, str):
                    finish_reason = reason
        return StreamChunk(
            text=text,
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

    def _log_done(self, event: str, started: float, usage: Usage | None) -> None:
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
        self._report_usage(usage, latency_ms)

    def _report_usage(self, usage: Usage | None, latency_ms: float) -> None:
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


def _serialize_message(message: ChatMessage) -> dict[str, object]:
    data: dict[str, object] = {"role": message.role.value, "content": message.content}
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
    )


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
