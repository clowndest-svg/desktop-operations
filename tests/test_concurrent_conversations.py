"""Several conversations at once, and the turns that belong to them.

The old shape of this code was one conversation per process: one replay list, one citation
list, one model choice, all of them fields on :class:`ChatService`. It was safe only
because nothing could ask two questions at once -- which stopped being true the moment the
bridge answered a second tab, since pywebview runs **every** JS API call on its own thread
and the phone's HTTP server runs one thread per request.

So these tests hold two turns open at the same time and look for what leaks. A fake that
releases on a gate is the whole trick: without a real overlap there is no race to fail, and
every "isolation" test written against sequential calls passes whether or not the code
isolates anything.

The second theme is attribution. Three conversations on three models is only worth having
if a later reader can tell who said what and what it cost, which is why ``model`` travels
from the reply into the transcript row, and ``task_id`` travels into the usage ledger.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from typing import Any, cast

from jarvis.app.chat_service import ChatReply, ChatService, ConversationTuning, ThinkingRequest
from jarvis.app.conversation import STATUS_DONE, STATUS_IDLE
from jarvis.app.transcript_service import TranscriptService
from jarvis.app.turns import (
    KIND_CHAT,
    MAX_KEPT,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_QUEUED,
    STATE_RUNNING,
    TurnRegistry,
)
from jarvis.database import SqliteStore
from jarvis.llm.types import ChatResponse, GenerationOptions, ToolCall, Usage
from jarvis.tools.types import ToolResult
from tests._fakes import FakeLlmClient

_TOOL_CALL = ToolCall(id="call-1", name="lookup", arguments="{}")
"""The one tool call the two-round client asks for.

Given an id and raw JSON because that is the wire's shape: the loop feeds the result back
as ``ChatMessage.tool_result(call.id, ...)``, and a test double that skipped the id would
not be exercising the pairing the model depends on.
"""


class HeldClient:
    """Answers only when told to, so two turns really do overlap.

    The gate is the point. A test that asks A, waits, then asks B proves nothing about
    concurrent state -- it passes with every shared field still shared.
    """

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.released = threading.Event()
        self.arrived = threading.Condition()
        self.seen: list[list[str]] = []
        self.options: list[GenerationOptions | None] = []
        self.used: list[str] = []
        self.provider_name = "held"
        self.model = "held-model"

    def complete(self, messages: Any, *, options: GenerationOptions | None = None) -> ChatResponse:
        question = str(messages[-1].content)
        self.used.append("complete")
        with self.arrived:
            self.seen.append([str(message.content) for message in messages])
            self.options.append(options)
            self.arrived.notify_all()
        self.released.wait(timeout=5.0)
        answer = next((text for key, text in self._answers.items() if key in question), "答")
        return ChatResponse(content=answer, model=self.model, usage=Usage(11, 3))

    def stream(self, messages: Any, *, options: GenerationOptions | None = None) -> Any:
        return iter(())

    def wait_until_asked(self, index: int, timeout: float = 5.0) -> bool:
        """Whether at least ``index`` requests have reached the client."""
        deadline = time.monotonic() + timeout
        with self.arrived:
            while len(self.seen) < index:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.arrived.wait(left)
            return True


def _service(client: Any, **kwargs: Any) -> ChatService:
    service = ChatService(client if callable(client) else lambda: client, **kwargs)
    service.start()
    return service


def _transcript(tmp_path: Path) -> TranscriptService:
    store = SqliteStore(tmp_path / "chat.db")
    store.start()
    transcript = TranscriptService(store)
    transcript.start()
    return transcript


class TestTwoConversationsDoNotShareState:
    def test_each_tab_replays_only_its_own_history(self, tmp_path: Any) -> None:
        client = HeldClient({"第一个问题": "第一个答案", "第二个问题": "第二个答案"})
        service = _service(client, transcript=_transcript(cast(Path, tmp_path)), session_id="tab-a")
        other = str(service.new_conversation()["conversation"])
        assert other and other != "tab-a"

        first = threading.Thread(target=lambda: service.ask("第一个问题", conversation="tab-a"))
        second = threading.Thread(target=lambda: service.ask("第二个问题", conversation=other))
        first.start()
        second.start()
        assert client.wait_until_asked(2), "两轮没有真的重叠，这条测试就是空的"
        client.released.set()
        first.join(timeout=5)
        second.join(timeout=5)

        asked = client.seen
        assert len(asked) == 2
        one, two = asked
        # Each request carried only its own question as history -- the system prompt plus
        # one user turn. A shared replay list would put the other tab's sentence in here.
        assert sum(1 for content in one if content.startswith("第一个问题")) == 1
        assert not any(content.startswith("第二个问题") for content in one)
        assert sum(1 for content in two if content.startswith("第二个问题")) == 1
        assert not any(content.startswith("第一个问题") for content in two)

    def test_two_first_turns_open_two_sessions_not_one_split_session(self, tmp_path: Any) -> None:
        """The old check-then-act on ``_session_id`` let two first turns both create a
        session and both claim it, so one conversation's answers landed under the other."""
        client = FakeLlmClient("答")
        transcript = _transcript(cast(Path, tmp_path))
        service = _service(client, transcript=transcript)
        tab_b = service.new_conversation()["conversation"]

        service.ask("甲的问题", conversation="tab-a")  # the id the service started with
        service.ask("乙的问题", conversation=str(tab_b))

        sessions = transcript.list_sessions()
        assert len(sessions) == 2, sessions
        titles = sorted(str(row["title"]) for row in sessions)
        assert titles == ["乙的问题", "甲的问题"], titles

    def test_citations_belong_to_the_turn_that_retrieved_them(self, tmp_path: Any) -> None:
        """``sources`` used to be one field on the service, written at retrieval and read
        after the answer -- which is a two-line recipe for showing one tab's 来源 under
        another tab's answer."""

        class TwoHit:
            def __init__(self) -> None:
                self.calls = 0

            def retrieve(self, _query: str) -> list[Any]:
                self.calls += 1
                if self.calls == 1:
                    return [_Hit("手册.pdf 第 1 段")]
                return []

            def stats(self) -> dict[str, object]:
                return {"chunks": 12}

        class _Hit:
            def __init__(self, citation: str) -> None:
                self.citation_value = citation
                self.text = "原文"

            def citation(self) -> str:
                return self.citation_value

        knowledge = TwoHit()
        service = _service(FakeLlmClient("答"), knowledge_provider=lambda: cast(Any, knowledge))
        first = service.ask("有依据的问题")
        second = service.ask("没依据的问题")
        assert first.sources == ("手册.pdf 第 1 段",)
        assert second.sources == ()
        assert first.grounded is True and second.grounded is False


class TestPerConversationModel:
    def test_a_named_pair_is_asked_and_a_blank_one_follows_the_window(self) -> None:
        global_client = FakeLlmClient("全局的答案")
        chosen = FakeLlmClient("deepseek 的答案")
        built: list[tuple[str, str]] = []

        def client_for(provider: str, model: str) -> Any:
            built.append((provider, model))
            return chosen

        service = _service(
            lambda: global_client,
            client_for=client_for,
            pair_provider=lambda: ("qwen", "qwen-max"),
        )
        pinned = service.new_conversation()["conversation"]
        assert service.set_conversation_model(str(pinned), "deepseek", "deepseek-chat")["ok"]
        # ``new_conversation`` makes that tab the one on screen, so the earlier one has to
        # be named -- which is exactly how the page addresses a background tab.
        service.new_conversation()
        service.focus("hud")

        assert service.ask("甲", conversation=str(pinned)).model == "deepseek-chat"
        assert service.ask("乙", conversation="hud").model == "qwen-max"
        # Only the tab that *named* a model goes through the per-model builder. A tab
        # that inherited the window's choice keeps using the window's client -- otherwise
        # every single-tab conversation would start depending on machinery the console
        # path never wires up, and a missing pair would turn into an error on screen.
        assert built == [("deepseek", "deepseek-chat")]
        assert len(global_client.calls) == 1

    def test_thinking_and_context_are_read_per_pair_not_per_process(self) -> None:
        seen: list[GenerationOptions | None] = []

        class Recorder(FakeLlmClient):
            def complete(self, messages: Any, *, options: Any = None) -> ChatResponse:
                seen.append(options)
                return super().complete(messages, options=options)

        def tuning(provider: str, model: str) -> ConversationTuning:
            if provider == "deepseek":
                return ConversationTuning(ThinkingRequest(True, 4096), 3)
            return ConversationTuning(ThinkingRequest(True, 512), 30)

        service = _service(
            Recorder("答"),
            client_for=lambda provider, model: cast(Any, Recorder("答")),
            pair_provider=lambda: ("qwen", "qwen-max"),
            tuning_for=tuning,
        )
        pinned = service.new_conversation()["conversation"]
        service.set_conversation_model(str(pinned), "deepseek", "deepseek-chat")
        service.ask("甲", conversation=str(pinned))
        service.ask("乙", conversation="hud")

        assert seen[0] is not None and seen[1] is not None
        assert seen[0].thinking_budget == 4096, seen[0]
        assert seen[1].thinking_budget == 512, seen[1]


class TestModelAttributionAndCost:
    def test_the_answering_model_is_stored_with_the_answer(self, tmp_path: Any) -> None:
        transcript = _transcript(cast(Path, tmp_path))
        service = _service(
            FakeLlmClient("星期六"),
            transcript=transcript,
            session_id="tab-1",
            pair_provider=lambda: ("qwen", "qw"),
        )
        reply = service.ask("今天星期几")
        assert reply.answer == "星期六", reply

        rows = transcript.whole_session("tab-1")
        assert [row["model"] for row in rows] == ["", "qw"], rows

    def test_a_turn_that_called_tools_keeps_every_rounds_trace(self) -> None:
        """Reasoning and tokens are folded across rounds, not taken from the last one.

        A turn that called two tools thought before each of them and paid for three
        requests; showing only the final round is the version of this that made the
        思考过程 box useless and understates what the turn cost.
        """
        calls: list[int] = []

        class TwoRound:
            provider_name = "two"
            model = "two"

            def __init__(self) -> None:
                self.count = 0

            def complete(self, messages: Any, *, options: Any = None) -> ChatResponse:
                self.count += 1
                calls.append(self.count)
                if self.count == 1:
                    return ChatResponse(
                        content="",
                        model="two",
                        tool_calls=(_TOOL_CALL,),
                        usage=Usage(100, 5),
                        reasoning="第一轮：该查一下",
                    )
                return ChatResponse(
                    content="答", model="two", usage=Usage(200, 7), reasoning="第二轮：照着答"
                )

            def stream(self, messages: Any, *, options: Any = None) -> Any:
                return iter(())

        class Tools:
            enabled = True

            def to_openai_tools(self) -> list[dict[str, object]]:
                return [{"type": "function", "function": {"name": "lookup"}}]

            def invoke(self, name: str, arguments: dict[str, object]) -> Any:
                assert name == "lookup", name
                return ToolResult(tool=name, ok=True, output="查到了")

        client = TwoRound()
        service = _service(lambda: client, tool_provider=lambda: cast(Any, Tools()))
        reply = service.ask("问题")

        assert calls == [1, 2], "工具轮之后必须再问一次，否则没有答案可言"
        assert reply.answer == "答"
        assert reply.reasoning == "第一轮：该查一下\n\n第二轮：照着答", reply.reasoning
        assert reply.tools_used == ("lookup",)


def test_the_reply_carries_which_conversation_it_belongs_to() -> None:
    """A late answer has to be filed under the tab that asked, not the one on screen."""
    service = _service(FakeLlmClient("答"))
    tab = service.new_conversation()["conversation"]
    reply = service.ask("甲", conversation=str(tab))
    assert reply.conversation == str(tab)
    assert ChatReply("甲", "答", conversation="t").to_dict()["conversation"] == "t"


class DribblingClient:
    """Streams a canned answer token by token, the way the real client does.

    :class:`HeldClient` above has no ``complete_stream`` on purpose -- that is what
    makes it exercise the fallback. This one is the other half: increments arrive, the
    stop flag is polled between them, and a caller who walks out gets a partial back.
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.provider_name = "dribble"
        self.model = "dribble-model"
        self.used: list[str] = []
        self.stop_polls = 0

    @property
    def partials(self) -> list[str]:
        return [" ".join(self.tokens[: index + 1]) for index in range(len(self.tokens))]

    def complete(self, messages: Any, *, options: GenerationOptions | None = None) -> ChatResponse:
        del messages, options
        self.used.append("complete")
        return ChatResponse(content=" ".join(self.tokens), model=self.model, usage=Usage(7, 2))

    def complete_stream(
        self,
        messages: Any,
        *,
        options: GenerationOptions | None = None,
        on_text: Any = None,
        should_stop: Any = None,
    ) -> ChatResponse:
        del messages, options
        self.used.append("complete_stream")
        parts: list[str] = []
        for token in self.tokens:
            parts.append(token)
            if on_text is not None:
                on_text(" ".join(parts))
            self.stop_polls += 1
            if should_stop is not None and should_stop():
                return ChatResponse(
                    content=" ".join(parts), model=self.model, finish_reason="cancelled"
                )
        return ChatResponse(content=" ".join(parts), model=self.model, finish_reason="stop")

    def stream(self, messages: Any, *, options: GenerationOptions | None = None) -> Any:
        del messages, options
        return iter(())


class TestAWatchedTurnStreamsAndAStoppedOneStops:
    """The two things the operator was promised: see it think, and cut it off."""

    def test_the_partial_answers_arrive_growing_and_the_last_one_is_the_answer(self) -> None:
        client = DribblingClient(["今天", "多云", "18 度"])
        service = _service(client)
        seen: list[str] = []

        reply = service.ask("天气", on_delta=seen.append)

        assert seen == ["今天", "今天 多云", "今天 多云 18 度"]
        assert reply.answer == "今天 多云 18 度"
        assert client.used == ["complete_stream"]

    def test_a_turn_nothing_is_watching_is_answered_in_one_block(self) -> None:
        """The voice path and the phone pass no callbacks and get no stream.

        Streaming costs a second code path in the client, and a spoken answer is read
        aloud from the finished text either way -- so "nobody is looking at the screen"
        has to be enough to choose the plain route, not just an optimisation.
        """
        client = DribblingClient(["答"])
        service = _service(client)
        assert service.ask("天气").answer == "答"
        assert client.used == ["complete"]

    def test_a_stop_in_the_middle_returns_what_had_been_said(self) -> None:
        client = DribblingClient(["一", "二", "三", "四"])
        service = _service(client)
        asked = {"n": 0}

        def stop() -> bool:
            asked["n"] += 1
            return asked["n"] > 2

        reply = service.ask("讲四个", should_stop=stop)

        assert reply.answer == "一 二 三"
        assert reply.cancelled is True

    def test_a_stopped_turn_is_not_recorded_as_a_failure(self) -> None:
        """Stopping is an answer with less in it, not an error the tab has to show.

        The distinction survives into the panel: a failed tab reads 「出错」 and offers a
        retry, while a stopped one keeps the half-sentence the operator watched arrive.
        """
        client = DribblingClient(["一", "二"])
        service = _service(client)
        tab = str(service.new_conversation()["conversation"])
        reply = service.ask("说吧", conversation=tab, should_stop=lambda: True)
        assert reply.cancelled is True
        assert reply.answer == "一"
        assert reply.error == ""
        assert service.conversation(tab).status == STATUS_DONE

    def test_a_client_that_cannot_stream_still_says_something_out_loud(self) -> None:
        """ "This provider can't stream" is a log line, not a silent downgrade.

        The answer still comes, so nothing looks broken from the outside -- which is
        exactly when a note has to exist, because the symptom the operator then reports
        is "停止 没用".
        """
        client = HeldClient({"甲": "乙的答案"})
        service = _service(client)
        assert service.ask("甲", should_stop=lambda: False).answer == "乙的答案"
        assert client.used == ["complete"]

    def test_a_stop_before_a_non_streaming_answer_is_honoured_at_the_boundary(self) -> None:
        client = HeldClient({"甲": "乙的答案"})
        service = _service(client)
        reply = service.ask("甲", should_stop=lambda: True)
        assert reply.cancelled is True
        assert reply.answer == ""
        assert client.used == [], "点了停止就不该再花一次请求"


class TestTurnRegistry:
    def test_a_running_turn_is_listed_and_a_cancelled_one_reports_cancelled(self) -> None:
        inside = threading.Event()
        finish = threading.Event()
        registry = TurnRegistry()

        def work(context: Any) -> dict[str, object]:
            inside.set()
            assert context.should_stop() is False
            finish.wait(timeout=5)
            assert context.should_stop() is True
            return {"answer_chars": 3, "state": STATE_DONE}

        info = registry.start(conversation_id="tab", kind=KIND_CHAT, question="问", work=work)
        assert inside.wait(timeout=5)
        assert [task.task_id for task in registry.running()] == [info.task_id]
        assert registry.recent()[0].state == STATE_RUNNING

        assert registry.cancel(info.task_id)["ok"] is True
        finish.set()
        assert registry.wait(info.task_id, 5.0)
        assert registry.recent()[0].state == STATE_CANCELLED

    def test_cancelling_a_finished_turn_says_so_instead_of_claiming_success(self) -> None:
        registry = TurnRegistry()
        info = registry.start(
            conversation_id="t", kind=KIND_CHAT, question="问", work=lambda _c: {"answer_chars": 1}
        )
        assert registry.wait(info.task_id, 5.0)
        answer = registry.cancel(info.task_id)
        assert answer["ok"] is False and answer["error"]

    def test_a_turn_that_raises_is_failed_not_lost(self) -> None:
        registry = TurnRegistry()

        def boom(_context: Any) -> dict[str, object]:
            raise RuntimeError("模型断了")

        info = registry.start(conversation_id="t", kind=KIND_CHAT, question="问", work=boom)
        assert registry.wait(info.task_id, 5.0)
        finished = registry.get(info.task_id)
        assert finished is not None
        assert finished.state == STATE_CANCELLED or "模型断了" in finished.error
        assert finished.error, "失败了却没有原因，等于让人去猜"

    def test_the_table_announces_the_turn_before_its_thread_says_anything(self) -> None:
        """「现在在跑什么」从请求被接受那一刻起就是真的。

        标签卡片和宠物头上那张「思考中」都挂在这条通知上。要是等服务发出第一个阶段才算
        数，卡亮不亮就取决于工作线程有没有跑在主线程前面 —— 那是调度，不是设计。
        """
        gate = threading.Event()
        seen: list[tuple[str, str]] = []
        registry = TurnRegistry(on_update=lambda info: seen.append((info.state, info.phase)))

        def work(_context: Any) -> dict[str, object]:
            assert gate.wait(timeout=5.0), "测试还没放行就跑了"
            return {"answer_chars": 1}

        registry.start(conversation_id="t", kind=KIND_CHAT, question="问", work=work)
        assert seen and seen[0] == (STATE_RUNNING, "思考中"), seen
        gate.set()

    def test_listeners_see_the_phase_change_that_the_stop_button_needs(self) -> None:
        seen: list[tuple[str, str]] = []
        inside = threading.Event()
        release = threading.Event()
        registry = TurnRegistry(on_update=lambda info: seen.append((info.state, info.phase)))

        def work(context: Any) -> dict[str, object]:
            context.phase("调用工具")
            inside.set()
            release.wait(timeout=5)
            return {"answer_chars": 0}

        info = registry.start(conversation_id="t", kind=KIND_CHAT, question="问", work=work)
        assert inside.wait(timeout=5)
        release.set()
        assert registry.wait(info.task_id, 5.0)
        assert ("running", "调用工具") in seen, seen
        assert seen[-1][0] == STATE_DONE

    def test_an_update_listener_that_throws_does_not_eat_the_answer(self) -> None:
        def broken(_info: Any) -> None:
            raise RuntimeError("界面断了")

        registry = TurnRegistry(on_update=broken)
        info = registry.start(
            conversation_id="t",
            kind=KIND_CHAT,
            question="问",
            work=lambda _c: {"answer_chars": 7},
        )
        assert registry.wait(info.task_id, 5.0)
        stored = registry.get(info.task_id)
        assert stored is not None and stored.answer_chars == 7


class TestOneConversationAnswersOneQuestionAtATime:
    """同一个会话里连着问，第二句要**等**第一句答完 —— 但不是全局排队。

    操作者要的是"提出的任务可以不断叠加"：话一句一句发出去，她按发出去的顺序一句一句答。
    排队只按会话排，两个标签页同时问还是并行 —— 要保护的是"一段对话的记录不被两轮同时
    改写"，不是模型的额度。
    """

    def test_the_second_question_waits_and_never_touches_the_model_early(self) -> None:
        ran: list[str] = []
        gate = threading.Event()
        registry = TurnRegistry()

        def make(tag: str) -> Any:
            def work(_context: Any) -> dict[str, object]:
                ran.append(tag)
                assert gate.wait(timeout=5.0), "测试还没放行就跑了"
                return {"answer_chars": 1}

            return work

        first = registry.start(
            conversation_id="t", kind=KIND_CHAT, question="第一问", work=make("第一问")
        )
        second = registry.start(
            conversation_id="t", kind=KIND_CHAT, question="第二问", work=make("第二问")
        )
        third = registry.start(
            conversation_id="t", kind=KIND_CHAT, question="第三问", work=make("第三问")
        )
        assert first.state == STATE_RUNNING
        assert second.state == STATE_QUEUED, "同会话第二句不该立刻开跑"
        assert third.state == STATE_QUEUED
        assert [task.task_id for task in registry.queued("t")] == [
            second.task_id,
            third.task_id,
        ], "队要按发出去的顺序排"
        assert registry.running("t") and len(registry.running("t")) == 1
        gate.set()
        assert registry.wait(first.task_id, 5.0)
        assert registry.wait(second.task_id, 5.0)
        assert registry.wait(third.task_id, 5.0)
        assert ran == ["第一问", "第二问", "第三问"], ran
        assert registry.queued("t") == (), "队里不该留尾巴"

    def test_two_conversations_still_overlap(self) -> None:
        """排队是按会话的：两个标签页各问各的，不该互相等。"""
        inside = threading.Event()
        release = threading.Event()
        registry = TurnRegistry()

        def work(_context: Any) -> dict[str, object]:
            inside.set()
            assert release.wait(timeout=5.0)
            return {"answer_chars": 1}

        a = registry.start(conversation_id="a", kind=KIND_CHAT, question="甲", work=work)
        b = registry.start(conversation_id="b", kind=KIND_CHAT, question="乙", work=work)
        assert a.state == STATE_RUNNING and b.state == STATE_RUNNING
        assert inside.wait(timeout=5)
        assert len(registry.running()) == 2
        release.set()
        assert registry.wait(a.task_id, 5.0) and registry.wait(b.task_id, 5.0)

    def test_a_waiting_question_can_be_withdrawn_and_then_is_never_asked(self) -> None:
        """撤销的那句不该在下一轮答完之后被补问出来。"""
        ran: list[str] = []
        gate = threading.Event()
        registry = TurnRegistry()

        def make(tag: str) -> Any:
            def work(_context: Any) -> dict[str, object]:
                ran.append(tag)
                assert gate.wait(timeout=5.0)
                return {"answer_chars": 1}

            return work

        first = registry.start(
            conversation_id="t", kind=KIND_CHAT, question="第一问", work=make("第一问")
        )
        second = registry.start(
            conversation_id="t", kind=KIND_CHAT, question="撤回的那问", work=make("撤回")
        )
        answer = registry.cancel(second.task_id)
        assert answer["ok"] is True, "排队中的任务也必须停得掉"
        withdrawn = registry.get(second.task_id)
        assert withdrawn is not None and withdrawn.state == STATE_CANCELLED
        gate.set()
        assert registry.wait(first.task_id, 5.0)
        time.sleep(0.05)
        assert ran == ["第一问"], f"撤掉的问题还是被问了：{ran}"
        assert registry.queued("t") == ()

    def test_the_card_stays_lit_across_the_handover(self) -> None:
        """第一句答完、第二句还没起来的空档里，「思考中」不能闪一下灭掉。

        桥面是按 ``waiting()``（在跑的 + 在排的）决定她头上那张卡的，所以这里钉的是通知
        序列：中间必须有一次把第二句报成 running，而不是先 done 再什么都不发。
        """
        seen: list[tuple[str, str]] = []
        gate = threading.Event()
        registry = TurnRegistry(on_update=lambda info: seen.append((info.state, info.phase)))

        def work(_context: Any) -> dict[str, object]:
            assert gate.wait(timeout=5.0)
            return {"answer_chars": 1}

        first = registry.start(conversation_id="t", kind=KIND_CHAT, question="问一", work=work)
        second = registry.start(conversation_id="t", kind=KIND_CHAT, question="问二", work=work)
        assert (STATE_QUEUED, "排队中") in seen, seen
        assert len(registry.waiting("t")) == 2, "在跑 + 在排都算欠人一个回答"
        gate.set()
        assert registry.wait(first.task_id, 5.0)
        assert registry.wait(second.task_id, 5.0)
        states = [state for state, _phase in seen]
        assert states == [
            STATE_RUNNING,
            STATE_QUEUED,
            STATE_DONE,
            STATE_RUNNING,
            STATE_DONE,
        ], seen

    def test_a_synchronous_caller_waits_through_the_queue_or_says_it_timed_out(self) -> None:
        """手机那一路是在调用里等答案的：排在队里时不许返回一个空回答。"""
        gate = threading.Event()
        registry = TurnRegistry()

        def work(_context: Any) -> dict[str, object]:
            assert gate.wait(timeout=5.0)
            return {"answer_chars": 1}

        first = registry.start(conversation_id="t", kind=KIND_CHAT, question="问一", work=work)
        second = registry.start(conversation_id="t", kind=KIND_CHAT, question="问二", work=work)
        assert registry.wait(second.task_id, 0.2) is False, "还在排队就说没等到"
        gate.set()
        assert registry.wait(first.task_id, 5.0)
        assert registry.wait(second.task_id, 5.0)

    def test_a_full_table_never_forgets_a_waiting_question(self) -> None:
        """淘汰旧轮次时不许把队里那句一起忘掉 —— 面板上还列着它。"""
        gate = threading.Event()
        registry = TurnRegistry()

        def hold(_context: Any) -> dict[str, object]:
            assert gate.wait(timeout=5.0)
            return {"answer_chars": 1}

        busy = registry.start(conversation_id="t", kind=KIND_CHAT, question="在跑", work=hold)
        waiting = registry.start(
            conversation_id="t",
            kind=KIND_CHAT,
            question="在排",
            work=lambda _c: {"answer_chars": 1},
        )
        for index in range(MAX_KEPT + 10):
            done = registry.start(
                conversation_id=f"other{index}",
                kind=KIND_CHAT,
                question="填表的",
                work=lambda _c: {"answer_chars": 1},
            )
            assert registry.wait(done.task_id, 5.0)
        assert registry.get(waiting.task_id) is not None, "排队的问题被裁掉了"
        gate.set()
        assert registry.wait(busy.task_id, 5.0)
        assert registry.wait(waiting.task_id, 5.0)


class TestConversationLifecycle:
    def test_deleting_the_open_tab_opens_another_and_forgets_the_deleted_one(
        self, tmp_path: Any
    ) -> None:
        transcript = _transcript(cast(Path, tmp_path))
        service = _service(FakeLlmClient("答"), transcript=transcript)
        service.ask("问题")
        opened = service.session_id
        assert service.delete_session(opened)["current"] != opened
        assert all(row["id"] != opened for row in service.conversations())

    def test_an_unasked_tab_leaves_no_empty_session_behind(self, tmp_path: Any) -> None:
        """Opening and abandoning a tab must not litter 历史.

        The row is created with the first turn, which is also why the id is fixed at
        creation: a tab whose id changed when its first answer landed would leave the
        running turn writing into a session nobody is showing.
        """
        transcript = _transcript(cast(Path, tmp_path))
        service = _service(lambda: FakeLlmClient("答"), transcript=transcript)
        service.new_conversation()
        service.new_conversation()
        assert transcript.list_sessions() == []

    def test_a_status_survives_until_the_next_question(self) -> None:
        service = _service(lambda: (_ for _ in ()).throw(AssertionError("不该被问到")))
        card = next(row for row in service.conversations() if row["active"])
        assert card["status"] == STATUS_IDLE

    def test_clearing_a_storeless_tab_blanks_only_that_tab(self) -> None:
        client = FakeLlmClient("答")
        service = _service(client)
        first = service.new_conversation()["conversation"]
        service.ask("甲", conversation=str(first))
        service.clear_history(conversation=str(first))
        assert service.conversation(str(first)).history == []


def test_the_service_still_answers_without_any_of_the_new_wiring() -> None:
    """The console path: no transcript, no registry, no per-pair adapters."""
    client = FakeLlmClient("星期六")
    service = _service(client)
    reply = service.ask("今天星期几")
    assert reply.answer == "星期六"
    assert dataclasses.is_dataclass(reply)
