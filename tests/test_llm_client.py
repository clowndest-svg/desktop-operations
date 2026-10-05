"""Unit tests for the OpenAI-compatible client (fake transport, no network)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace

import pytest

from jarvis.core.events import UsageEvent
from jarvis.llm.errors import (
    LlmAuthError,
    LlmError,
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
        self.sse_replies: list[list[str] | Iterator[str] | Exception] = []
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
        self.json_calls.append(
            {"url": url, "headers": dict(headers), "payload": dict(payload), "timeout": timeout}
        )
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
        self.sse_calls.append(
            {"url": url, "headers": dict(headers), "payload": dict(payload), "timeout": timeout}
        )
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
    on_usage: Callable[[UsageEvent], None] | None = None,
    key_optional: bool = False,
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
        key_optional=key_optional,
    )
    recorded = sleeps if sleeps is not None else []
    return OpenAiCompatClient(
        settings, transport, sleep=recorded.append, environ=environ, on_usage=on_usage
    )


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

    def test_a_keyless_endpoint_is_called_without_an_authorization_header(self) -> None:
        """Ollama / LM Studio / llama.cpp answer the OpenAI protocol with no credential.

        Before this flag existed the client raised ``LlmAuthError`` before opening a
        socket, so a machine with a perfectly good local model could not be connected
        at all -- and the settings panel had been calling that field 「可选」 the whole time.
        """
        transport = FakeTransport()
        transport.json_replies.append(ok_response("本地正常"))

        response = make_client(transport, environ={}, key_optional=True).complete(
            [ChatMessage.user("hi")]
        )

        assert response.content == "本地正常"
        headers = transport.json_calls[0]["headers"]
        assert isinstance(headers, dict)
        assert "Authorization" not in headers, "没有钥匙就别发一个空的 Bearer"

    def test_a_keyless_endpoint_still_sends_the_key_when_somebody_sets_one(self) -> None:
        """LM Studio can be configured to require a password; the flag must not blind us."""
        transport = FakeTransport()
        transport.json_replies.append(ok_response())

        make_client(transport, environ=ENV, key_optional=True).complete([ChatMessage.user("hi")])

        headers = transport.json_calls[0]["headers"]
        assert isinstance(headers, dict)
        assert headers.get("Authorization") == "Bearer sk-unit-test"

    def test_keyless_is_per_provider_and_off_by_default(self) -> None:
        """A flag on one row must not quietly unlock the cloud provider next to it."""
        transport = FakeTransport()
        with pytest.raises(LlmAuthError):
            make_client(transport, environ={}).complete([ChatMessage.user("hi")])
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
        # The trailing usage-only chunk is yielded as well. Whoever assembles a reply
        # cannot tell "the model spent 800 tokens thinking" from "the provider never
        # said" unless the chunk that says it arrives -- and dropping it is how every
        # 用量 number goes missing for streamed turns, which is now most of them.
        assert [chunk.finish_reason for chunk in chunks if chunk.finish_reason] == ["stop"]
        assert chunks[-1].usage is not None
        assert chunks[-1].usage.prompt_tokens == 10
        payload = transport.sse_calls[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}

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


def sse_reasoning(text: str) -> str:
    delta: dict[str, object] = {"reasoning_content": text}
    return json.dumps({"choices": [{"delta": delta, "finish_reason": None}]})


def sse_tool_fragment(fragment: dict[str, object]) -> str:
    delta: dict[str, object] = {"tool_calls": [fragment]}
    return json.dumps({"choices": [{"delta": delta, "finish_reason": None}]})


def leaking_stream(lines: list[str], closed: list[str]) -> Iterator[str]:
    """A scripted SSE stream that says so when its consumer walks away.

    Stands in for the socket: the whole point of a stop button is that nobody keeps
    reading the answer after it is pressed, and a test that cannot tell "finished" from
    "abandoned" cannot show that the button does anything.
    """
    try:
        yield from lines
    finally:
        closed.append("closed")


def dying_stream(lines: list[str], error: Exception) -> Iterator[str]:
    """A stream that dies partway through: the connection drops after an answer began.

    The error has to be raised from inside the generator, since that is the only way to
    put a failure *between* two chunks -- which is the position where retrying stops
    being safe.
    """
    yield from lines
    raise error


class TestCompleteStream:
    """The path every HUD turn now takes, and the only one a 停止 button can act on.

    :meth:`OpenAiCompatClient.complete` hands back one opaque block after twenty
    seconds and leaves no handle to pull away, so streaming is not a presentation
    preference here -- it is what makes both advertised behaviours (watch it think,
    stop it mid-thought) possible at all.
    """

    def test_the_callback_sees_the_whole_partial_every_time(self) -> None:
        """Deltas are state, not increments.

        The UI pump coalesces snapshots and is allowed to drop an intermediate one, so
        a renderer of increments would keep a hole in the answer forever.
        """
        transport = FakeTransport()
        transport.sse_replies.append([sse_chunk("你"), sse_chunk("好"), sse_chunk("！"), "[DONE]"])
        seen: list[str] = []
        response = make_client(transport).complete_stream(
            [ChatMessage.user("hi")], on_text=seen.append
        )
        assert seen == ["你", "你好", "你好！"]
        assert response.content == "你好！"
        assert response.finish_reason == "stop" or response.finish_reason is None

    def test_a_stop_request_lands_within_one_chunk_and_keeps_the_partial(self) -> None:
        transport = FakeTransport()
        transport.sse_replies.append([sse_chunk(c) for c in "abcdefgh"] + ["[DONE]"])
        asked: list[bool] = []

        def stop_after_two_chunks() -> bool:
            asked.append(True)
            return len(asked) > 2

        response = make_client(transport).complete_stream(
            [ChatMessage.user("hi")], on_text=lambda _: None, should_stop=stop_after_two_chunks
        )
        assert response.content == "abc"
        assert response.finish_reason == "cancelled"
        assert len(asked) == 3

    def test_stopping_walks_away_from_the_socket(self) -> None:
        """The client-side half of 停止: abandoning the iterator reaches the transport.

        What the caller can promise is only this -- it stops asking for the next chunk
        and the stream it was handed is closed on the way out. That the closed stream is
        then a closed *connection* is the transport's promise, and it is pinned where it
        belongs, in ``test_llm_transport.py``. Both ends matter: a stop that left the
        client reading to the end would keep paying for an answer nobody watches.
        """
        transport = FakeTransport()
        closed: list[str] = []
        transport.sse_replies.append(
            leaking_stream([sse_chunk("a"), sse_chunk("b"), sse_chunk("c"), "[DONE]"], closed)
        )
        response = make_client(transport).complete_stream(
            [ChatMessage.user("hi")], should_stop=lambda: True
        )
        assert response.content == "a"
        assert response.finish_reason == "cancelled"
        assert closed == ["closed"]

    def test_an_answer_that_ran_to_the_end_is_not_reported_as_abandoned(self) -> None:
        transport = FakeTransport()
        closed: list[str] = []
        transport.sse_replies.append(leaking_stream([sse_chunk("a"), "[DONE]"], closed))
        make_client(transport).complete_stream([ChatMessage.user("hi")])
        assert closed == ["closed"]

    def test_an_abandoned_turn_still_appears_in_the_ledger(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Cost of the stopped request is reported as unknown, never as nothing.

        Providers put ``usage`` in the last chunk, so a cancelled turn usually has no
        numbers to give. The line and the event still have to be written: a turn that
        cost a request and left no trace is indistinguishable from one never asked, and
        the panel's 无读数 depends on seeing that distinction.
        """
        events: list[UsageEvent] = []
        transport = FakeTransport()
        transport.sse_replies.append([sse_chunk("a"), sse_chunk("b"), "[DONE]"])
        client = make_client(transport, on_usage=events.append)
        with caplog.at_level(logging.INFO, logger="jarvis.llm.client"):
            client.complete_stream([ChatMessage.user("hi")], should_stop=lambda: True)
        assert next(r for r in caplog.records if "stream abandoned" in r.message) is not None
        assert events == []

    def test_a_stop_after_the_usage_chunk_still_bills_the_turn(self) -> None:
        transport = FakeTransport()
        usage_chunk = json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 5}}
        )
        events: list[UsageEvent] = []
        transport.sse_replies.append([sse_chunk("a"), usage_chunk, sse_chunk("b"), "[DONE]"])
        asked: list[bool] = []

        def stop_after_the_usage_arrives() -> bool:
            asked.append(True)
            return len(asked) > 2

        client = make_client(transport, on_usage=events.append)
        response = client.complete_stream(
            [ChatMessage.user("hi")], should_stop=stop_after_the_usage_arrives
        )
        assert response.finish_reason == "cancelled"
        assert response.usage is not None
        assert [e.prompt_tokens for e in events] == [40]

    def test_the_task_the_request_belongs_to_reaches_the_ledger(self) -> None:
        """Twelve requests answer one round-table task; only an id on each sorts them.

        Not a wire field: the payload must stay exactly as wide as the provider asked.
        """
        events: list[UsageEvent] = []
        transport = FakeTransport()
        usage_chunk = json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
        )
        transport.sse_replies.append([sse_chunk("a"), usage_chunk, "[DONE]"])
        client = make_client(transport, on_usage=events.append)
        client.complete_stream(
            [ChatMessage.user("hi")], options=GenerationOptions(task_id="task-7")
        )
        assert [e.task_id for e in events] == ["task-7"]
        payload = transport.sse_calls[0]["payload"]
        assert isinstance(payload, dict)
        assert "task_id" not in payload

    def test_reasoning_arrives_on_its_own_thread(self) -> None:
        transport = FakeTransport()
        transport.sse_replies.append(
            [sse_reasoning("先看温度"), sse_chunk("有点热"), sse_reasoning("再看湿度"), "[DONE]"]
        )
        response = make_client(transport).complete_stream([ChatMessage.user("hi")])
        assert response.reasoning == "先看温度\n\n再看湿度"
        assert response.content == "有点热"

    def test_tool_calls_arrive_in_pieces_and_leave_as_whole_calls(self) -> None:
        transport = FakeTransport()
        transport.sse_replies.append(
            [
                sse_tool_fragment(
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ),
                sse_tool_fragment({"index": 0, "function": {"arguments": '{"city"'}}),
                sse_tool_fragment({"index": 0, "function": {"arguments": ': "Beijing"}'}}),
                json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                "[DONE]",
            ]
        )
        response = make_client(transport).complete_stream([ChatMessage.user("weather?")])
        assert response.tool_calls == (
            ToolCall(id="call_1", name="get_weather", arguments='{"city": "Beijing"}'),
        )
        assert response.content == ""

    def test_a_provider_that_rejects_the_usage_block_is_re_asked_once_without_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``stream_options`` is a real 400 on strict endpoints, not a free extra.

        Remembers the answer: re-probing per turn would put a failed request in front of
        every single answer the operator gets.
        """
        transport = FakeTransport()
        transport.sse_replies = [
            TransportStatusError(400, "unknown field stream_options"),
            [sse_chunk("ok"), "[DONE]"],
        ]
        with caplog.at_level(logging.WARNING, logger="jarvis.llm.client"):
            response = make_client(transport).complete_stream([ChatMessage.user("hi")])
        assert response.content == "ok"
        first, second = (call["payload"] for call in transport.sse_calls)
        assert isinstance(first, dict) and isinstance(second, dict)
        assert first["stream_options"] == {"include_usage": True}
        assert "stream_options" not in second
        assert any("rejected the streaming request" in r.message for r in caplog.records)

    def test_a_provider_that_takes_no_streaming_at_all_answers_in_one_block(self) -> None:
        transport = FakeTransport()
        transport.sse_replies = [
            TransportStatusError(400, "streaming not supported"),
            TransportStatusError(400, "streaming not supported"),
        ]
        transport.json_replies = [ok_response("一次给完"), ok_response("一次给完")]
        client = make_client(transport)
        assert client.complete_stream([ChatMessage.user("hi")]).content == "一次给完"
        assert len(transport.sse_calls) == 2
        assert len(transport.json_calls) == 1
        transport.sse_calls.clear()
        assert client.complete_stream([ChatMessage.user("hi again")]).content == "一次给完"
        assert transport.sse_calls == []
        assert len(transport.json_calls) == 2

    def test_failing_after_half_an_answer_raises_instead_of_replaying(self) -> None:
        """Retrying here would put two halves of an answer on the screen.

        The screen has already been shown the first half, chunk by chunk.
        """
        transport = FakeTransport()
        transport.sse_replies.append(
            dying_stream([sse_chunk("前半句")], TransportStatusError(500, "boom"))
        )
        with pytest.raises(LlmError):
            make_client(transport).complete_stream([ChatMessage.user("hi")])
        assert len(transport.sse_calls) == 1

    def test_an_empty_stream_is_an_empty_answer_not_a_crash(self) -> None:
        transport = FakeTransport()
        transport.sse_replies.append(["[DONE]"])
        response = make_client(transport).complete_stream([ChatMessage.user("hi")])
        assert response.content == ""
        assert response.usage is None


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


class TestPerProviderTimeout:
    """One row's own timeout has to reach the socket, not just the settings screen."""

    def test_the_row_timeout_reaches_the_transport(self) -> None:
        transport = FakeTransport()
        transport.json_replies.append(ok_response())
        client = make_client(transport)
        client._settings = replace(client._settings, timeout_seconds=180.0)

        client.complete([ChatMessage.user("hi")])

        assert transport.json_calls[0]["timeout"] == 180.0

    def test_the_service_hands_out_the_row_timeout_not_the_global_one(self) -> None:
        from jarvis.config.schema import LlmSection, ModelSpec, ProviderSection
        from jarvis.llm.openai_compat import OpenAiCompatClient
        from jarvis.llm.service import LlmService

        section = LlmSection(
            default_provider="slow",
            timeout_seconds=60.0,
            max_retries=1,
            retry_backoff_seconds=0.0,
            providers={
                "slow": ProviderSection(
                    name="slow",
                    base_url="http://localhost:11434/v1",
                    models=(ModelSpec(id="qwen2.5:7b-instruct"),),
                    default_model="qwen2.5:7b-instruct",
                    api_key_env="SLOW_API_KEY",
                    cost_input_per_1m=0.0,
                    cost_output_per_1m=0.0,
                    timeout_seconds=240.0,
                )
            },
        )
        service = LlmService(lambda: section)
        service.start()

        client = service.client_for("slow")
        assert isinstance(client, OpenAiCompatClient)
        assert client._settings.timeout_seconds == 240.0
