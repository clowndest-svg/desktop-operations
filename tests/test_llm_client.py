"""Unit tests for the OpenAI-compatible client (fake transport, no network)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping

import pytest

from jarvis.llm.errors import (
    LlmAuthError,
    LlmRateLimitError,
    LlmRequestError,
    LlmResponseError,
    LlmTimeoutError,
)
from jarvis.llm.openai_compat import OpenAiCompatClient, OpenAiCompatSettings
from jarvis.llm.transport import TransportStatusError, TransportTimeoutError
from jarvis.llm.types import ChatMessage, GenerationOptions, Role, ToolCall, Usage

ENV = {"TEST_API_KEY": "sk-unit-test"}


class FakeTransport:
    """Scripted transport: pops one prepared reply (or exception) per call."""

    def __init__(self) -> None:
        self.json_replies: list[Mapping[str, object] | Exception] = []
        self.sse_replies: list[list[str] | Exception] = []
        self.json_calls: list[dict[str, object]] = []
        self.sse_calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, object]:
        self.json_calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        reply = self.json_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def post_sse(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Iterator[str]:
        self.sse_calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        reply = self.sse_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield from reply


def make_client(
    transport: FakeTransport,
    sleeps: list[float] | None = None,
    *,
    environ: Mapping[str, str] = ENV,
    max_retries: int = 2,
    cost_input_per_1m: float = 0.0,
    cost_output_per_1m: float = 0.0,
) -> OpenAiCompatClient:
    settings = OpenAiCompatSettings(
        provider_name="testai",
        base_url="https://api.test.example/v1",
        model="test-model",
        api_key_env="TEST_API_KEY",
        timeout_seconds=30.0,
        max_retries=max_retries,
        retry_backoff_seconds=0.5,
        cost_input_per_1m=cost_input_per_1m,
        cost_output_per_1m=cost_output_per_1m,
    )
    recorded = sleeps if sleeps is not None else []
    return OpenAiCompatClient(settings, transport, sleep=recorded.append, environ=environ)


def ok_response(content: str = "hello") -> dict[str, object]:
    return {
        "model": "test-model-2024",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 512, "completion_tokens": 128},
    }


class TestComplete:
    def test_success_returns_content_model_usage(self) -> None:
        transport = FakeTransport()
        transport.json_replies.append(ok_response("你好"))
        response = make_client(transport).complete([ChatMessage.user("hi")])
        assert response.content == "你好"
        assert response.model == "test-model-2024"
        assert response.finish_reason == "stop"
        assert response.usage is not None
        assert response.usage.total_tokens == 640

    def test_request_payload_and_auth_header(self) -> None:
        transport = FakeTransport()
        transport.json_replies.append(ok_response())
        client = make_client(transport)
        client.complete(
            [ChatMessage.system("be brief"), ChatMessage.user("hi")],
            options=GenerationOptions(temperature=0.2, max_tokens=100, top_p=0.9),
        )
        call = transport.json_calls[0]
        assert call["url"] == "https://api.test.example/v1/chat/completions"
        headers = call["headers"]
        assert isinstance(headers, dict)
        assert headers["Authorization"] == "Bearer sk-unit-test"
        payload = call["payload"]
        assert isinstance(payload, dict)
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0.2
        assert payload["max_tokens"] == 100
        assert payload["top_p"] == 0.9
        assert "stream" not in payload
        assert payload["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ]

    def test_tool_calls_are_parsed_and_serialized(self) -> None:
        transport = FakeTransport()
        transport.json_replies.append(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Beijing"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )
        client = make_client(transport)
        response = client.complete([ChatMessage.user("weather?")])
        assert response.tool_calls == (
            ToolCall(id="call_1", name="get_weather", arguments='{"city": "Beijing"}'),
        )
        assert response.content == ""

        # Round-trip: echo the assistant tool-call turn + tool result back.
        transport.json_replies.append(ok_response("晴"))
        assistant_turn = ChatMessage(
            role=Role.ASSISTANT, content="", tool_calls=response.tool_calls
        )
        client.complete(
            [
                ChatMessage.user("weather?"),
                assistant_turn,
                ChatMessage.tool_result("call_1", '{"temp": 30}'),
            ]
        )
        payload = transport.json_calls[1]["payload"]
        assert isinstance(payload, dict)
        messages = payload["messages"]
        assert isinstance(messages, list)
        assert messages[1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert messages[2] == {
            "role": "tool",
            "content": '{"temp": 30}',
            "tool_call_id": "call_1",
        }

    def test_empty_message_list_is_rejected_locally(self) -> None:
        transport = FakeTransport()
        with pytest.raises(LlmRequestError):
            make_client(transport).complete([])
        assert transport.json_calls == []

    def test_missing_api_key_fails_before_any_request(self) -> None:
        transport = FakeTransport()
        client = make_client(transport, environ={})
        with pytest.raises(LlmAuthError, match="TEST_API_KEY"):
            client.complete([ChatMessage.user("hi")])
        assert transport.json_calls == []

    def test_retries_429_with_exponential_backoff_then_succeeds(self) -> None:
        transport = FakeTransport()
        transport.json_replies = [
            TransportStatusError(429, "slow down"),
            TransportStatusError(429, "slow down"),
            ok_response(),
        ]
        sleeps: list[float] = []
        response = make_client(transport, sleeps).complete([ChatMessage.user("hi")])
        assert response.content == "hello"
        assert len(transport.json_calls) == 3
        assert sleeps == [0.5, 1.0]

    def test_timeout_exhausts_retries(self) -> None:
        transport = FakeTransport()
        transport.json_replies = [TransportTimeoutError("t/o")] * 3
        sleeps: list[float] = []
        with pytest.raises(LlmTimeoutError):
            make_client(transport, sleeps).complete([ChatMessage.user("hi")])
        assert len(transport.json_calls) == 3  # 1 + 2 retries
        assert sleeps == [0.5, 1.0]

    def test_auth_error_is_never_retried(self) -> None:
        transport = FakeTransport()
        transport.json_replies = [TransportStatusError(401, "bad key")]
        with pytest.raises(LlmAuthError):
            make_client(transport).complete([ChatMessage.user("hi")])
        assert len(transport.json_calls) == 1

    def test_bad_request_maps_to_request_error(self) -> None:
        transport = FakeTransport()
        transport.json_replies = [TransportStatusError(400, "unknown model")]
        with pytest.raises(LlmRequestError):
            make_client(transport).complete([ChatMessage.user("hi")])

    def test_rate_limit_surfaces_after_exhausted_retries(self) -> None:
        transport = FakeTransport()
        transport.json_replies = [TransportStatusError(429, "nope")] * 2
        with pytest.raises(LlmRateLimitError):
            make_client(transport, max_retries=1).complete([ChatMessage.user("hi")])

    def test_malformed_body_maps_to_response_error(self) -> None:
        transport = FakeTransport()
        transport.json_replies = [{"object": "error", "detail": "no choices here"}]
        with pytest.raises(LlmResponseError):
            make_client(transport).complete([ChatMessage.user("hi")])

    def test_metrics_are_logged_with_cost(self, caplog: pytest.LogCaptureFixture) -> None:
        transport = FakeTransport()
        transport.json_replies.append(ok_response())
        client = make_client(transport, cost_input_per_1m=1.0, cost_output_per_1m=2.0)
        with caplog.at_level(logging.INFO, logger="jarvis.llm.client"):
            client.complete([ChatMessage.user("hi")])
        record = next(r for r in caplog.records if "complete ok" in r.message)
        assert getattr(record, "tokens_in", None) == 512
        assert getattr(record, "tokens_out", None) == 128
        assert getattr(record, "latency_ms", None) is not None
        cost = getattr(record, "cost_usd", None)
        assert cost == pytest.approx((512 * 1.0 + 128 * 2.0) / 1_000_000)


def sse_chunk(text: str | None = None, finish: str | None = None) -> str:
    delta: dict[str, object] = {}
    if text is not None:
        delta["content"] = text
    choice: dict[str, object] = {"delta": delta, "finish_reason": finish}
    return json.dumps({"choices": [choice]})


class TestStream:
    def test_stream_yields_increments_finish_and_usage(self) -> None:
        transport = FakeTransport()
        usage_chunk = json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
        )
        transport.sse_replies.append(
            [sse_chunk("你"), sse_chunk("好"), sse_chunk(None, "stop"), usage_chunk, "[DONE]"]
        )
        chunks = list(make_client(transport).stream([ChatMessage.user("hi")]))
        assert "".join(chunk.text for chunk in chunks) == "你好"
        assert chunks[-1].finish_reason == "stop"
        payload = transport.sse_calls[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["stream"] is True

    def test_stream_retries_before_first_chunk(self) -> None:
        transport = FakeTransport()
        transport.sse_replies = [
            TransportStatusError(503, "warming up"),
            [sse_chunk("ok"), "[DONE]"],
        ]
        sleeps: list[float] = []
        chunks = list(make_client(transport, sleeps).stream([ChatMessage.user("hi")]))
        assert [chunk.text for chunk in chunks] == ["ok"]
        assert sleeps == [0.5]
        assert len(transport.sse_calls) == 2

    def test_stream_stops_at_done_sentinel(self) -> None:
        transport = FakeTransport()
        transport.sse_replies.append([sse_chunk("a"), "[DONE]", sse_chunk("ignored")])
        chunks = list(make_client(transport).stream([ChatMessage.user("hi")]))
        assert [chunk.text for chunk in chunks] == ["a"]

    def test_stream_bad_json_raises_response_error(self) -> None:
        transport = FakeTransport()
        transport.sse_replies.append(["{not json"])
        with pytest.raises(LlmResponseError):
            list(make_client(transport).stream([ChatMessage.user("hi")]))

    def test_stream_usage_reaches_metrics_log(self, caplog: pytest.LogCaptureFixture) -> None:
        transport = FakeTransport()
        usage_chunk = json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        )
        transport.sse_replies.append([sse_chunk("x"), usage_chunk, "[DONE]"])
        with caplog.at_level(logging.INFO, logger="jarvis.llm.client"):
            list(make_client(transport).stream([ChatMessage.user("hi")]))
        record = next(r for r in caplog.records if "stream ok" in r.message)
        assert getattr(record, "tokens_in", None) == 7
        assert getattr(record, "tokens_out", None) == 3


class TestCachedTokenParsing:
    """The cache number the statistics popup shows, and what "unknown" has to mean.

    Two wire shapes exist in the wild and neither implies the other: OpenAI-style
    providers nest the count under ``prompt_tokens_details``, Anthropic-style ones
    put it at the top level of ``usage``. Reading only one is how a provider that
    *does* report caching gets displayed as "no reading" forever.
    """

    @staticmethod
    def _with(usage: dict[str, object]) -> Usage | None:
        transport = FakeTransport()
        transport.json_replies.append(
            {
                "model": "test-model-2024",
                "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                "usage": usage,
            }
        )
        response = make_client(transport).complete([ChatMessage.user("hi")])
        return response.usage

    def test_openai_style_nested_field_is_read(self) -> None:
        usage = self._with(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 640},
            }
        )
        assert usage is not None
        assert usage.cached_tokens == 640
        assert usage.cache_hit_percent == pytest.approx(64.0)

    def test_anthropic_style_flat_field_is_read(self) -> None:
        usage = self._with(
            {"prompt_tokens": 800, "completion_tokens": 10, "cache_read_input_tokens": 200}
        )
        assert usage is not None
        assert usage.cached_tokens == 200

    def test_a_provider_that_says_nothing_yields_none_not_zero(self) -> None:
        """The whole honesty of 「无读数」 rests on this distinction."""
        usage = self._with({"prompt_tokens": 512, "completion_tokens": 128})
        assert usage is not None
        assert usage.cached_tokens is None
        assert usage.cache_hit_percent is None

    def test_a_genuine_zero_is_kept_as_zero(self) -> None:
        usage = self._with(
            {
                "prompt_tokens": 512,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        )
        assert usage is not None
        assert usage.cached_tokens == 0
        assert usage.cache_hit_percent == 0.0

    def test_a_boolean_is_not_a_token_count(self) -> None:
        usage = self._with(
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": True},
            }
        )
        assert usage is not None
        assert usage.cached_tokens is None

    def test_a_negative_cache_count_is_clamped_not_propagated(self) -> None:
        usage = self._with(
            {"prompt_tokens": 10, "completion_tokens": 1, "cache_read_input_tokens": -5}
        )
        assert usage is not None
        assert usage.cached_tokens == 0
