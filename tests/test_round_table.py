"""The round table: several models, one question, the same record in front of each.

The behaviour worth pinning is not "it calls N times" -- it is *what each seat is shown*.
A round table that leaks one model's private reasoning into another's, or that hands a
seat the previous answer as its own earlier message, is not a discussion at all: models
do not contradict their own voice. So most of these tests read the recorded prompts.

Cost accounting is the second theme. Three seats over two rounds is nine requests, and the
number the operator is owed is the sum of those nine -- including the merge, which is a
request too and is the easiest one to forget to bill.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jarvis.app.collaboration import (
    MODE_BOSS,
    MODE_TABLE,
    MODE_VOTE,
    Collaboration,
    label_for,
    requests_for,
)
from jarvis.app.round_table import (
    MAX_ROUNDS,
    MAX_SEATS,
    RoundResult,
    RoundTable,
    Seat,
    Speech,
    refuse_pictures,
)
from jarvis.core.exceptions import JarvisError
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, Role, Usage
from tests._fakes import FakeLlmClient

DEEP = Seat("deepseek", "deepseek-chat")
QWEN = Seat("qwen", "qwen-max")
GPT = Seat("openai", "gpt-4o")
THREE = [DEEP, QWEN, GPT]


class TableClient:
    """A client that records what it was asked and answers from a script."""

    def __init__(self, seat: Seat, recorder: Recorder, *, fail: bool = False) -> None:
        self._seat = seat
        self._recorder = recorder
        self._fail = fail
        self.provider_name = seat.provider
        self.model = seat.model

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        if self._fail:
            raise JarvisError(f"{self._seat.label} 挂了")
        self._recorder.asked.append((self._seat, list(messages), options))
        text = self._recorder.next_text(self._seat)
        return ChatResponse(content=text, model=self.model, usage=Usage(10, 2))

    def stream(self, messages: Any, *, options: Any = None) -> Any:
        return iter(())


class Recorder:
    """Who spoke, in what order, with what in front of them."""

    def __init__(
        self,
        words: dict[str, str] | None = None,
        *,
        failing: tuple[str, ...] = (),
        streaming: bool = False,
    ):
        self.asked: list[tuple[Seat, list[ChatMessage], GenerationOptions | None]] = []
        self.words = words or {}
        self.failing = failing
        self.streaming = streaming
        self.counter = 0

    def client_for(self, provider: str, model: str) -> TableClient:
        seat = Seat(provider, model)
        kind = StreamingTableClient if self.streaming else TableClient
        return kind(seat, self, fail=seat.model in self.failing)

    def next_text(self, seat: Seat) -> str:
        if seat.label in self.words:
            return self.words[seat.label]
        self.counter += 1
        return f"{seat.model} 第 {self.counter} 次发言"

    @property
    def prompts(self) -> list[str]:
        """The last message of every request: the ask each seat was given."""
        return [messages[-1].content for _, messages, _ in self.asked]

    @property
    def roles(self) -> list[list[str]]:
        return [[message.role.value for message in messages] for _, messages, _ in self.asked]

    @property
    def options(self) -> list[GenerationOptions | None]:
        return [options for _, _, options in self.asked]


def _table(recorder: Recorder, **kwargs: Any) -> RoundTable:
    return RoundTable(recorder.client_for, **kwargs)


class TestTheOrderAndTheRecord:
    def test_every_seat_is_asked_once_per_round_and_the_merge_is_last(self) -> None:
        recorder = Recorder()
        result = _table(recorder).run("怎么定这个价", THREE, rounds=1)

        asked = [seat.model for seat, _, _ in recorder.asked]
        assert asked == ["deepseek-chat", "qwen-max", "gpt-4o", "deepseek-chat"]
        assert result.rounds == 2, "一轮讨论 + 一轮合并"
        assert len(result.speeches) == 4

    def test_a_seat_sees_every_speech_made_before_it_and_none_after(self) -> None:
        recorder = Recorder()
        _table(recorder).run("问题", THREE, rounds=1)

        second, third, merge = recorder.prompts[1], recorder.prompts[2], recorder.prompts[3]
        assert "deepseek-chat 第 1 次发言" in second
        assert "qwen-max" not in second
        assert "qwen-max 第 2 次发言" in third
        # The merge reads the whole table, and the last seat cannot see the merge it is
        # about to produce.
        assert all(word in merge for word in ("第 1 次发言", "第 2 次发言", "第 3 次发言"))

    def test_the_record_is_quoted_text_not_somebody_elses_transcript(self) -> None:
        """No assistant messages from the other seats.

        A foreign answer replayed as ``assistant`` reads to the next model as its own
        earlier words, and models do not argue with themselves -- which would turn the
        table into a copy machine.
        """
        recorder = Recorder()
        _table(recorder).run("问题", THREE, rounds=1)

        for seat, messages, _ in recorder.asked:
            assert [message.role for message in messages].count(Role.ASSISTANT) == 0, seat
            assert messages[-1].role is Role.USER

    def test_the_first_seat_is_told_it_is_first(self) -> None:
        recorder = Recorder()
        _table(recorder).run("问题", THREE, rounds=1)
        assert "还没有人发言" in recorder.prompts[0]
        assert "还没有人发言" not in recorder.prompts[1]

    def test_the_merge_is_asked_for_a_conclusion_not_another_opinion(self) -> None:
        recorder = Recorder()
        _table(recorder).run("问题", THREE, rounds=1)
        assert "合并" in recorder.prompts[-1]
        assert "不要复述别人的话" not in recorder.prompts[-1]

    def test_the_record_reaches_the_result_and_the_answer_does_not_repeat_it(self) -> None:
        recorder = Recorder()
        result = _table(recorder).run("问题", THREE, rounds=1)
        assert "第 1 次发言" in result.record
        assert result.answer == result.speeches[-1].text
        assert result.answer not in result.record, "结论是合并出来的，不是记录里抄来的"
        assert result.speeches[-1].round_index == result.rounds

    def test_two_rounds_mean_everyone_speaks_again_before_the_merge(self) -> None:
        recorder = Recorder()
        result = _table(recorder).run("问题", THREE, rounds=2)
        assert len([seat for seat, _, _ in recorder.asked]) == 3 * 2 + 1
        assert {speech.round_index for speech in result.speeches} == {1, 2, 3}


class TestTheBill:
    def test_the_task_id_is_on_every_request_of_the_table(self) -> None:
        """Nine requests, one task. Anything else and 这花了多少 has no answer.

        The ledger can group by time or by model; neither says "this task". That is what
        the id is for, and it has to be attached to the requests, not inferred later.
        """
        recorder = Recorder()
        _table(recorder).run("问题", THREE, rounds=1, task_id="task-9")
        assert [options.task_id for options in recorder.options if options] == ["task-9"] * 4

    def test_the_cost_of_the_table_adds_up_over_every_speech_including_the_merge(
        self,
    ) -> None:
        recorder = Recorder()
        result = _table(recorder).run("问题", THREE, rounds=1)
        assert len(result.speeches) == 4
        assert result.prompt_tokens == 40
        assert result.completion_tokens == 8

        rows = [speech.to_dict() for speech in result.speeches]
        assert rows and all(row["model"] for row in rows)
        shown = sum(
            row["prompt_tokens"] if isinstance(row["prompt_tokens"], int) else 0 for row in rows
        )
        assert shown == result.prompt_tokens, "页面看到的每一段，加起来要等于总数"

    def test_no_speech_means_no_number_rather_than_a_zero(self) -> None:
        empty = RoundResult(answer="", record="")
        assert empty.prompt_tokens is None
        assert empty.to_dict()["prompt_tokens"] is None


class TestTheCaps:
    def test_a_table_too_big_for_its_own_good_is_refused_before_any_request(self) -> None:
        recorder = Recorder()
        seats = [Seat(f"p{i}", f"m{i}") for i in range(MAX_SEATS + 2)]
        result = _table(recorder).run("问题", seats, rounds=1)
        assert result.error
        assert str(MAX_SEATS) in result.error
        assert recorder.asked == [], "超员不该花一分钱"

    def test_rounds_cannot_be_talked_past_the_ceiling(self) -> None:
        recorder = Recorder()
        result = _table(recorder).run("问题", [DEEP, QWEN], rounds=99)
        assert result.rounds == MAX_ROUNDS + 1

    def test_an_empty_seat_list_is_said_out_loud(self) -> None:
        recorder = Recorder()
        result = _table(recorder).run("问题", [], rounds=1)
        assert "没有选模型" in result.error
        assert recorder.asked == []


class TestStoppingAndFailure:
    def test_a_stop_between_two_seats_keeps_everything_the_table_already_said(self) -> None:
        recorder = Recorder()
        flips = {"n": 0}

        def stop_after_two() -> bool:
            flips["n"] += 1
            return flips["n"] > 2

        result = _table(recorder).run("问题", THREE, rounds=2, should_stop=stop_after_two)

        assert result.stopped is True
        assert len(result.speeches) == 2
        assert len(recorder.asked) == 2, "点了停止就不该再问第三家"
        assert result.answer == result.speeches[-1].text

    def test_one_silent_seat_ends_the_table_with_its_name_on_the_record(self) -> None:
        recorder = Recorder(failing=("qwen-max",))
        result = _table(recorder).run("问题", THREE, rounds=1)
        assert not result.ok
        assert "qwen-max" in result.error
        assert len(result.speeches) == 1

    def test_a_table_whose_merge_failed_still_hands_over_the_discussion(self) -> None:
        recorder = Recorder(failing=("deepseek-chat",))
        result = _table(recorder).run("问题", [DEEP, QWEN], rounds=1)
        # deepseek speaks first and fails, so nothing was said at all: the answer is empty,
        # but the operator is told which chair was the problem rather than seeing a blank.
        assert result.speeches == ()
        assert "deepseek" in result.error

    def test_the_merge_blowing_up_falls_back_to_the_last_word(self) -> None:
        # Only the merge ask comes from the first seat's model *after* everyone spoke, so
        # failing that model fails the merge alone.
        recorder = Recorder()

        class MergeFails(RoundTable):
            def _speak(self, question: str, seat: Seat, **kwargs: Any) -> Speech | None:
                if kwargs.get("moderator"):
                    return None
                return super()._speak(question, seat, **kwargs)

        table = MergeFails(recorder.client_for)
        result = table.run("问题", THREE, rounds=1)
        assert "合并" in result.error
        assert result.answer == result.speeches[-1].text
        assert result.speeches[-1].seat == GPT
        assert len(result.speeches) == 3, "三家都说了话，只是没人把它们并起来"


class TestTheVoiceOfTheTable:
    def test_a_speech_streams_its_own_words_and_not_the_previous_ones(self) -> None:
        """Each seat restarts the partial text.

        The streaming bubble is one widget shared by the whole table, so a partial that
        carried over from the last seat would read as one model finishing another's
        sentence -- which is the opposite of what a round table is for.
        """
        recorder = Recorder(streaming=True)
        seen: list[str] = []
        result = _table(recorder).run("问题", [DEEP, QWEN], rounds=1, on_delta=seen.append)

        # Two seats plus the merge, four words each. Every speech starts over: the first
        # partial of a seat is that seat's own first word, never the previous seat's
        # sentence with one more word bolted on the end.
        assert len(seen) == 3 * 4
        first_words = [partial for index, partial in enumerate(seen) if index % 4 == 0]
        assert first_words == ["deepseek-chat", "qwen-max", "deepseek-chat"]
        assert result.speeches[0].text.startswith("deepseek-chat")

    def test_a_stop_in_the_middle_of_a_speech_keeps_that_sentences_words(self) -> None:
        recorder = Recorder(streaming=True)
        flips = {"n": 0}

        def stop_mid_first_speech() -> bool:
            flips["n"] += 1
            return flips["n"] > 2

        result = _table(recorder).run(
            "问题",
            [DEEP, QWEN],
            rounds=1,
            on_delta=lambda _partial: None,
            should_stop=stop_mid_first_speech,
        )
        assert len(recorder.asked) == 1, "掐掉之后不该再问第二家"
        assert result.stopped is True
        assert result.speeches[-1].text == "deepseek-chat 第"

    def test_the_listener_being_broken_does_not_silence_the_table(self) -> None:
        recorder = Recorder()

        def bad_listener(speech: Speech) -> None:
            raise RuntimeError("界面挂了")

        result = _table(recorder).run("问题", [DEEP, QWEN], rounds=1, on_speech=bad_listener)
        assert len(result.speeches) == 3


class TestReadingSeatsFromThePage:
    def test_a_seat_is_a_pair_not_a_display_string(self) -> None:
        assert Seat.of({"provider": "deepseek", "model": "deepseek-chat"}) == DEEP
        assert Seat.of({"model": " 只给了模型 "}) == Seat("", "只给了模型")
        assert Seat.of({"provider": "", "model": ""}) is None
        assert Seat.of("deepseek") is None
        assert Seat.of(None) is None


class TestTheServiceRunsTheTable:
    """:meth:`ChatService.ask_together` -- the same conversation, the asking shared.

    The point of these is what lands where: the merged answer is what the next question
    replays, the record of who said what is only what the operator reads, and a picture
    is refused rather than handed to two of three seats.
    """

    def _service(self, recorder: Recorder, **kwargs: Any) -> Any:
        from jarvis.app.chat_service import ChatService

        service = ChatService(
            lambda: recorder.client_for("any", "thing"),
            client_for=recorder.client_for,
            collaboration=Collaboration(recorder.client_for),
            **kwargs,
        )
        service.start()
        return service

    def test_the_conversation_is_shared_with_the_plain_path(self, tmp_path: Any) -> None:
        from jarvis.app.transcript_service import TranscriptService
        from jarvis.database import SqliteStore

        recorder = Recorder()
        store = SqliteStore(tmp_path / "chat.db")
        store.start()
        transcript = TranscriptService(store)
        transcript.start()
        service = self._service(recorder, transcript=transcript)

        first = service.ask_together(
            "怎么定价", [{"provider": "deepseek", "model": "deepseek-chat"}]
        )
        second = service.ask("那第二个方案呢")

        assert first.answer
        rows = transcript.whole_session(transcript.list_sessions()[0]["id"])
        texts = [str(row["content"]) for row in rows]
        assert texts == ["怎么定价", first.answer, "那第二个方案呢", second.answer]
        assert first.record not in texts, "讨论记录不进历史，只有结论进"

        # The next plain question replays the conclusion, so the table is not a side
        # conversation the tab forgets the moment it answers alone again.
        replayed = recorder.asked[-1][1]
        assert any(message.content == first.answer for message in replayed)

    def test_the_page_gets_the_record_and_the_answer_separately(self) -> None:
        recorder = Recorder()
        service = self._service(recorder)
        reply = service.ask_together("问题", [DEEP, QWEN])
        payload = reply.to_dict()
        assert payload["answer"] == reply.answer
        assert "第 1 次发言" in payload["record"]
        assert payload["model"] == DEEP.model, "合并的那一家是署名的一家"

    def test_a_picture_at_the_table_is_refused_before_anyone_is_asked(self) -> None:
        recorder = Recorder()
        service = self._service(recorder)
        reply = service.ask_together(
            "看这张图",
            [DEEP, QWEN],
            attachments=[
                {
                    "name": "a.png",
                    "kind": "image",
                    "mime": "image/png",
                    "size": 9,
                    "data": "data:image/png;base64,AAAA",
                }
            ],
        )
        assert "不带图" in reply.error
        assert recorder.asked == [], "拒了就一分钱也别花"

    def test_the_evidence_is_retrieved_once_and_every_seat_reads_it(self) -> None:
        recorder = Recorder()

        calls: list[str] = []

        class Knowledge:
            def retrieve(self, query: str) -> list[Any]:
                calls.append(query)
                return [_Hit()]

        service = self._service(recorder, knowledge_provider=lambda: Knowledge())
        reply = service.ask_together("手册里怎么写的", [DEEP, QWEN], rounds=1)

        assert len(calls) == 1, "检索一次就够，三家读的是同一份"
        assert reply.sources and len(set(reply.sources)) == 1
        assert len(recorder.asked) == 3
        for prompt in recorder.prompts:
            assert "手册.pdf 第 3 段" in prompt, "每把椅子上都要放着同一份检索结果"

    def test_no_table_wired_is_an_answer_not_a_crash(self) -> None:
        from jarvis.app.chat_service import ChatService

        service = ChatService(lambda: FakeLlmClient("答"))
        service.start()
        assert "协作" in service.ask_together("问题", [DEEP]).error

    def test_the_strip_says_who_has_the_floor(self) -> None:
        """The tab's one-line status names the model that is talking, in order.

        Three bubbles all reading 「思考中」 would be the same amount of information as
        one -- and whose turn it is, is the one thing a table has that a single model
        does not.
        """
        recorder = Recorder()
        service = self._service(recorder)
        seen: list[tuple[str, int, str]] = []
        tab = str(service.new_conversation()["conversation"])

        def note(seat: Any, round_index: int) -> None:
            seen.append((str(seat.label), round_index, str(service.conversation(tab).phase)))

        service.ask_together("问题", [DEEP, QWEN], conversation=tab, rounds=1, on_floor=note)

        assert [(label, index) for label, index, _ in seen] == [
            (DEEP.label, 1),
            (QWEN.label, 1),
            (DEEP.label, 2),
        ]
        assert DEEP.label in seen[0][2] and "正在发言" in seen[0][2]
        assert QWEN.label not in seen[0][2], "轮到谁就只写谁"

    def test_a_stopped_table_reports_cancelled_and_keeps_the_answer(self) -> None:
        recorder = Recorder()
        service = self._service(recorder)
        reply = service.ask_together("问题", [DEEP, QWEN], should_stop=lambda: True)
        assert reply.cancelled is True
        assert reply.answer == ""


class TestThePageOpensATable:
    """The bridge door: it returns at once, and the question is on screen first."""

    def test_the_task_starts_immediately_and_the_question_is_already_up(
        self, tmp_path: Any
    ) -> None:
        from jarvis.app.chat_service import ChatService
        from jarvis.app.turns import KIND_ROUNDTABLE, TurnRegistry
        from jarvis.ui.state_bridge import StateBridge
        from tests.test_ui_desktop import _bridge

        recorder = Recorder()
        chat = ChatService(
            lambda: recorder.client_for("a", "b"),
            client_for=recorder.client_for,
            collaboration=Collaboration(recorder.client_for),
        )
        chat.start()
        state = StateBridge()
        turns = TurnRegistry()
        bridge = _bridge(tmp_path, chat=chat, state=state, turns=turns)

        answer = bridge.chat_collaborate(
            "怎么定这个价", [{"provider": "deepseek", "model": "deepseek-chat"}]
        )

        assert answer["ok"] is True and answer["task_id"]
        assert answer["answer"] == "", "答得完再回来就不是圆桌了，页面等不起"
        assert turns.wait(str(answer["task_id"]), 5.0) is True
        # "确实结过束"只能在 wait 之后断言。这是一扇异步的门：主线程抢在工人线程前面
        # 是常态，放在前面就是一条靠运气通过的计时测试（2026-10-04 连跑三次红一次）。
        assert not turns.running(), "任务要真的收过尾，不许留成永远 running 的僵尸"
        listed = turns.get(str(answer["task_id"]))
        assert listed is not None and listed.kind == KIND_ROUNDTABLE
        assert listed.participants == (DEEP.label,)
        roles = [turn.role for turn in state.snapshot().history]
        assert roles == ["user", "assistant"], "问题先上、结论后上，两个都在"
        assert chat.history_length == 1, "一张圆桌只留一条问答进历史"
        # Two rounds by default plus the merge, and every one of them tagged with the
        # task -- 这轮花了多少 is only answerable if the seats bill to the same id.
        assert len(recorder.asked) == 3
        assert [options.task_id for options in recorder.options if options is not None] == [
            answer["task_id"]
        ] * 3

    def test_an_empty_seat_list_is_refused_before_the_question_goes_up(self, tmp_path: Any) -> None:
        from jarvis.app.chat_service import ChatService
        from tests.test_ui_desktop import _bridge

        chat = ChatService(
            lambda: FakeLlmClient("答"),
            collaboration=Collaboration(lambda p, m: FakeLlmClient("答")),
        )
        chat.start()
        bridge = _bridge(tmp_path, chat=chat)
        answer = bridge.chat_collaborate("问题", [])
        assert "至少" in str(answer["error"])

    def test_a_table_without_the_task_table_says_why(self, tmp_path: Any) -> None:
        """No registry means no way to stop it, and that is worth refusing over."""
        from jarvis.app.chat_service import ChatService
        from tests.test_ui_desktop import _bridge

        chat = ChatService(
            lambda: FakeLlmClient("答"),
            collaboration=Collaboration(lambda p, m: FakeLlmClient("答")),
        )
        chat.start()
        bridge = _bridge(tmp_path, chat=chat)
        answer = bridge.chat_collaborate("问题", [{"model": "m"}], conversation="hud")
        assert "任务表" in str(answer.get("error") or "")


FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"


class TestThePageAndTheTableAgree:
    """The page counts requests to show a price, and the table decides the real cap.

    Two numbers written in two languages drift, and this one is on the customer's bill: a
    panel advertising "4家×3轮" that the backend silently clamps to two rounds is showing
    a price it will not charge and an outcome it will not produce.
    """

    def _page(self, relative: str = "components/ChatPanel.vue") -> str:
        return (FRONTEND / relative).read_text(encoding="utf-8")

    def test_the_mode_names_are_the_same_four_on_both_sides(self) -> None:
        """下拉框里的值和后端 `COLLABORATION_MODES` 必须是同一批。

        漂了的后果是静默的：页面发一个后端不认的 mode，`chat_collaborate` 直接退回
        "不认识的协作形态"，用户看到的是下拉框选了个没用。
        """
        from jarvis.app.collaboration import COLLABORATION_MODES

        api = self._page("api/bridge.ts")
        for mode in COLLABORATION_MODES:
            assert f"'{mode}'" in api, f"页面没有 {mode}"
        assert "chatRoundTable" not in api and "chat_round_table" not in api
        assert "chat_collaborate(" in api, "桥面没这扇门，下拉框就是死的"
        assert '@change="setMode"' in self._page("components/ChatPanel.vue")

    def test_the_price_the_page_shows_is_the_price_the_engine_pays(self) -> None:
        """`requestsFor` 和 `requests_for` 是同一条算式的两份手写实现。

        这是页面上唯一替用户算钱的地方，错了不会有人发现 —— 直到用量面板给出另一个数。
        """
        api = self._page("api/bridge.ts")
        assert "if (mode === 'boss') return seats + 1" in api
        assert "if (mode === 'vote') return seats * 2" in api
        for seats, rounds in ((3, 2), (4, 1), (2, 3)):
            for mode in (MODE_TABLE, MODE_BOSS, MODE_VOTE):
                assert requests_for(mode, seats, rounds) > 0, mode
        assert requests_for(MODE_BOSS, 3, 2) == 4
        assert requests_for(MODE_VOTE, 3, 2) == 6
        # 形态的人话名字两边都得有，缺一个下拉框就出现英文 key。
        for mode in (MODE_TABLE, MODE_BOSS, MODE_VOTE):
            assert label_for(mode) in api, label_for(mode)

    def test_the_seat_and_round_ceilings_match_the_backends(self) -> None:
        page = self._page()
        assert f"const MAX_SEATS = {MAX_SEATS}" in page
        assert f"const MAX_ROUNDS = {MAX_ROUNDS}" in page

    def test_the_door_the_button_presses_exists_on_both_sides(self) -> None:
        page = self._page()
        assert "chatCollaborate" in page
        api = (FRONTEND / "api" / "bridge.ts").read_text(encoding="utf-8")
        assert "chat_collaborate(" in api, "桥面上没这扇门，按钮就是死的"
        assert "llm_test(" in api

    def test_the_record_has_its_own_fold_rather_than_joining_the_answer(self) -> None:
        page = self._page()
        assert "圆桌记录" in page
        assert "line.record" in page

    def test_a_pasted_picture_is_refused_locally_as_well_as_remotely(self) -> None:
        """The service refuses the combination; the page must not send it and look ignored.

        Silent is the failure mode this project keeps being told about, and a picture that
        vanishes into a table that never mentions it is exactly that.
        """
        assert "协作模式不带图" in self._page()


class _Hit:
    """One retrieved fragment, in the shape the service reads."""

    text = "定价按成本加成，先看竞品"

    def citation(self) -> str:
        return "手册.pdf 第 3 段"


def test_pictures_are_refused_with_a_way_out() -> None:
    assert refuse_pictures(DEEP, []) == ""
    note = refuse_pictures(DEEP, ["data:image/png;base64,xx"])
    assert "不带图" in note
    assert "单个模型" in note


class StreamingTableClient(TableClient):
    """A seat that can be watched and interrupted mid-sentence."""

    def complete_stream(
        self,
        messages: Any,
        *,
        options: Any = None,
        on_text: Any = None,
        should_stop: Any = None,
    ) -> ChatResponse:
        if self._fail:
            raise JarvisError("nope")
        self._recorder.asked.append((self._seat, list(messages), options))
        text = self._recorder.next_text(self._seat)
        parts: list[str] = []
        for word in text.split(" "):
            parts.append(word)
            if on_text is not None:
                on_text(" ".join(parts))
            if should_stop is not None and should_stop():
                return ChatResponse(
                    content=" ".join(parts),
                    model=self.model,
                    usage=Usage(10, 2),
                    finish_reason="cancelled",
                )
        return ChatResponse(content=text, model=self.model, usage=Usage(10, 2))
