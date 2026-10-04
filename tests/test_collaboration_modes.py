"""主管分发 与 并行三答·互评投票：谁能看见什么，以及票是怎么数出来的。

三种协作形态只差一件事 —— **谁能看见谁说的话**。所以这里大部分断言读的是 prompt，
不是返回值：

* 主管模式下 worker 只能看见分给自己的那一条。它要是看见了别人的，分工就退化成讨论，
  价格却还按分工报 —— 没人会告诉你这笔账变了。
* 投票模式下评审看到的必须是没有署名的答案，编号还要每次重排。模型认得出自己的文风，
  但至少认不出名字；不匿名的那套不叫投票，叫点名。

假件的答话故意写成 `回答 #N` 这种中性句子：一旦带上模型名，"表上不许有名字"那条断言
就在测假件而不是测代码。

第二条主线是**账要平**：拆单没拆够几家、票读不出来、并列，全都要在记录里留一行，
而不是被静默补全或悄悄丢掉。见 [[feedback-stat-additivity]]、
[[feedback-failures-must-be-visible]]。
"""

from __future__ import annotations

import random
from collections.abc import MutableSequence, Sequence
from typing import Any

from jarvis.app.collaboration import (
    MODE_BOSS,
    MODE_SINGLE,
    MODE_TABLE,
    MODE_VOTE,
    BossTable,
    Collaboration,
    VoteTable,
    requests_for,
)
from jarvis.app.round_table import MAX_SEATS, Seat
from jarvis.llm.types import ChatMessage, ChatResponse, GenerationOptions, Usage

A = Seat("deepseek", "deepseek-chat")
B = Seat("qwen", "qwen-max")
C = Seat("openai", "gpt-4o")

TASK_MARK = "序号|要做的事"
BALLOT_MARK = "答案|理由"

TASK_1 = "查价格弹性"
TASK_2 = "算单位成本"
SPLIT_2 = f"1|{TASK_1}\n2|{TASK_2}"


class OrderedShuffle(random.Random):
    """A shuffle that keeps the order, so a tally can be asserted instead of guessed."""

    def shuffle(self, sequence: MutableSequence[Any], *args: Any) -> None:
        del sequence, args


class ModeClient:
    """A client that reads which job it was given from the wording of the ask.

    Routing on the prompt rather than on call order is what keeps the visibility
    assertions honest: the fake cannot know which seat is next, so a prompt that leaked
    another seat's task shows up in the recorded ask instead of being smoothed over by a
    script that assumed the order.
    """

    def __init__(self, seat: Seat, recorder: ModeRecorder, *, fail: bool = False) -> None:
        self._seat = seat
        self._recorder = recorder
        self._fail = fail
        self.provider_name = seat.provider
        self.model = seat.model

    def complete(
        self, messages: Sequence[ChatMessage], *, options: GenerationOptions | None = None
    ) -> ChatResponse:
        if self._fail:
            raise RuntimeError(f"{self._seat.label} 没答")
        ask = messages[-1].content
        self._recorder.asked.append((self._seat, ask))
        return ChatResponse(
            content=self._recorder.script(self._seat, ask),
            model=self.model,
            usage=Usage(11, 3),
        )

    def stream(self, messages: Any, *, options: Any = None) -> Any:
        return iter(())


class ModeRecorder:
    """Every ask in order, plus the words each seat answers with."""

    def __init__(self, *, ballots: list[str] | None = None, split: str = SPLIT_2) -> None:
        self.asked: list[tuple[Seat, str]] = []
        self.ballots = ballots if ballots is not None else []
        self.split = split
        self._ballot = 0
        self._n = 0

    def client_for(self, provider: str, model: str) -> ModeClient:
        return ModeClient(Seat(provider, model), self)

    def script(self, seat: Seat, ask: str) -> str:
        if TASK_MARK in ask:
            return self.split
        if BALLOT_MARK in ask:
            pick = self.ballots[self._ballot % max(1, len(self.ballots))]
            self._ballot += 1
            return pick
        self._n += 1
        return f"回答 #{self._n}"

    @property
    def prompts(self) -> list[str]:
        return [ask for _, ask in self.asked]

    def asks_from(self, seat: Seat) -> list[str]:
        return [ask for who, ask in self.asked if who.label == seat.label]


class FlakyRecorder(ModeRecorder):
    """One named seat that cannot answer at all."""

    def __init__(self, broken: Seat, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._broken = broken

    def client_for(self, provider: str, model: str) -> ModeClient:
        seat = Seat(provider, model)
        return ModeClient(seat, self, fail=seat.label == self._broken.label)


class TestThePrice:
    def test_each_shape_costs_what_the_page_says_it_costs(self) -> None:
        assert requests_for(MODE_TABLE, 3, 2) == 9
        assert requests_for(MODE_BOSS, 3, 2) == 4, "拆 1 + 干 2 + 收 1：主管不领活"
        assert requests_for(MODE_VOTE, 3, 2) == 6
        assert requests_for(MODE_SINGLE, 1) == 1
        assert requests_for(MODE_BOSS, 0) == 0, "一家没选就不该报个价"

    def test_the_shapes_actually_make_that_many_calls(self) -> None:
        """The number on the page is not a guess about the engine's appetite."""
        recorder = ModeRecorder()
        BossTable(recorder.client_for).run("问题", [A, B, C])
        assert len(recorder.asked) == requests_for(MODE_BOSS, 3)

        asked = ModeRecorder()
        VoteTable(asked.client_for, shuffle=OrderedShuffle()).run("问题", [A, B, C])
        assert len(asked.asked) == requests_for(MODE_VOTE, 3)


class TestTheSupervisorSplit:
    def test_a_worker_sees_only_its_own_slice(self) -> None:
        recorder = ModeRecorder()
        BossTable(recorder.client_for).run("怎么定价", [A, B, C])

        assert len(recorder.asks_from(B)) == 1, "主管模式下每家只该被问一次"
        assert TASK_1 in recorder.asks_from(B)[0]
        assert TASK_2 not in recorder.asks_from(B)[0], "别人的活漏进来，分工就成了讨论"
        assert TASK_2 in recorder.asks_from(C)[0]
        assert TASK_1 not in recorder.asks_from(C)[0]

    def test_no_worker_sees_another_workers_answer(self) -> None:
        recorder = ModeRecorder()
        BossTable(recorder.client_for).run("怎么定价", [A, B, C])
        for ask in recorder.prompts[1:3]:
            assert "回答 #" not in ask, "worker 之间不许互相看答案，那是圆桌"

    def test_the_collector_reads_every_subanswer(self) -> None:
        recorder = ModeRecorder()
        BossTable(recorder.client_for).run("怎么定价", [A, B, C])
        last = recorder.prompts[-1]
        for marker in (TASK_1, TASK_2, "回答 #1", "回答 #2"):
            assert marker in last
        assert "原问题" not in last or "怎么定价" in last

    def test_the_first_chair_splits_and_collects_and_never_works(self) -> None:
        recorder = ModeRecorder()
        BossTable(recorder.client_for).run("问题", [A, B, C])
        assert [seat.label for seat, _ in recorder.asked] == [A.label, B.label, C.label, A.label]
        assert len(recorder.asks_from(A)) == 2

    def test_a_supervisor_that_refuses_to_split_is_announced_not_guessed_for(self) -> None:
        """Prose instead of ``序号|任务`` is this shape's native failure mode.

        Hard-failing loses the split request already paid for; silently guessing hands a
        worker a task nobody read. So the repair is announced into the record and every
        seat falls back to the original question.
        """
        recorder = ModeRecorder(split="我觉得应该分工，但你要我怎么写我不管。")
        result = BossTable(recorder.client_for).run("怎么定价", [A, B, C])
        assert "没按「序号|要做的事」" in result.record
        assert not result.error, "这不是失败，是降级，而且降级写在脸上"
        assert all("怎么定价" in ask for ask in recorder.prompts[1:3])

    def test_a_short_split_is_topped_up_out_loud(self) -> None:
        recorder = ModeRecorder(split=f"1|{TASK_1}")
        result = BossTable(recorder.client_for).run("怎么定价", [A, B, C])
        assert "剩下 1 家按原问题作答" in result.record
        assert len(recorder.asked) == 4, "补上的那家照样要问"

    def test_a_long_split_drops_the_extra_tasks_saying_so(self) -> None:
        recorder = ModeRecorder(split=f"{SPLIT_2}\n3|多出来的一件\n4|还多一件")
        result = BossTable(recorder.client_for).run("怎么定价", [A, B, C])
        assert "多拆了 2 件" in result.record
        assert "多出来的一件" not in "".join(recorder.prompts[1:3])

    def test_a_supervisor_that_cannot_answer_stops_everything_before_any_work(
        self,
    ) -> None:
        recorder = FlakyRecorder(A)
        result = BossTable(recorder.client_for).run("问题", [A, B, C])
        assert A.label in result.error
        assert recorder.asked == [], "拆都没拆成，别先花干活的钱"


class TestTheAnonymousVote:
    def test_answers_are_written_blind(self) -> None:
        recorder = ModeRecorder()
        VoteTable(recorder.client_for, shuffle=OrderedShuffle()).run("怎么定价", [A, B, C])
        for ask in recorder.prompts[:3]:
            assert "回答 #" not in ask

    def test_a_ballot_sheet_carries_no_names(self) -> None:
        recorder = ModeRecorder()
        VoteTable(recorder.client_for, shuffle=OrderedShuffle()).run("怎么定价", [A, B, C])
        for sheet in recorder.prompts[3:]:
            for seat in (A, B, C):
                assert seat.model not in sheet and seat.provider not in sheet, "署名就等于点名"
            assert "答案一" in sheet and "答案二" in sheet and "答案三" in sheet

    def test_the_numbers_are_reshuffled_for_every_reviewer(self) -> None:
        """同一张顺序发给三个评审，位置偏好就原封不动地传染进了票面。"""
        recorder = ModeRecorder()
        VoteTable(recorder.client_for, shuffle=random.Random(11)).run("怎么定价", [A, B, C])
        sheets = recorder.prompts[3:]
        assert len(set(sheets)) > 1, "三次评审看到同一份顺序"

    def test_the_winner_is_handed_over_in_its_own_words(self) -> None:
        """The delivered answer is a model's answer, not a merge -- that is the deal."""
        recorder = ModeRecorder(ballots=["答案一|数据靠谱", "答案一|同意", "答案二|结构更好"])
        result = VoteTable(recorder.client_for, shuffle=OrderedShuffle()).run("怎么定价", [A, B, C])
        assert result.answer == "回答 #1"
        assert len(recorder.asked) == 6, "投票不再加一次合并的钱"
        assert "deepseek-chat 2 票" in result.record

    def test_an_unreadable_ballot_is_counted_as_abstention_not_thrown_away(self) -> None:
        """票面必须加得平：投出去的 + 弃权 == 评审次数。"""
        recorder = ModeRecorder(
            ballots=["这道题我也拿不准", f"答案{'二'}|看起来最稳", "答案二|同意前面"]
        )
        result = VoteTable(recorder.client_for, shuffle=OrderedShuffle()).run("怎么定价", [A, B, C])
        assert "弃权 1" in result.record
        assert "弃权（它说的是" in result.record, "弃权是哪一家、说了什么都得留下"
        assert result.answer == "回答 #2"
        votes = [line for line in result.record.splitlines() if "投给" in line]
        assert len(votes) == 2

    def test_a_tie_keeps_the_first_chair_and_says_it_was_a_tie(self) -> None:
        recorder = ModeRecorder(ballots=["答案一|理由", "答案二|理由", "答案三|理由"])
        result = VoteTable(recorder.client_for, shuffle=OrderedShuffle()).run("怎么定价", [A, B, C])
        assert "并列 1 票" in result.record
        assert result.answer == "回答 #1"

    def test_one_seat_failing_cancels_the_vote_and_names_it(self) -> None:
        recorder = FlakyRecorder(B)
        result = VoteTable(recorder.client_for).run("问题", [A, B, C])
        assert B.label in result.error
        assert len(recorder.asked) == 1, "第一家答完、第二家失败，就该停在这儿"

    def test_a_single_seat_votes_nothing_and_says_so(self) -> None:
        recorder = ModeRecorder()
        result = VoteTable(recorder.client_for).run("问题", [A])
        assert "没有可比的东西" in result.record
        assert len(recorder.asked) == 1


class TestTheDoorPicksTheShape:
    def test_a_fifth_chair_is_refused_before_anything_is_asked(self) -> None:
        recorder = ModeRecorder()
        seats = [Seat(f"p{i}", f"m{i}") for i in range(MAX_SEATS + 1)]
        result = Collaboration(recorder.client_for).run(MODE_VOTE, "问题", seats)
        assert str(MAX_SEATS) in result.error
        assert recorder.asked == []

    def test_a_boss_needs_somebody_to_order_about(self) -> None:
        recorder = ModeRecorder()
        result = Collaboration(recorder.client_for).run(MODE_BOSS, "问题", [A])
        assert "至少" in result.error and recorder.asked == []

    def test_single_is_not_a_collaboration(self) -> None:
        recorder = ModeRecorder()
        result = Collaboration(recorder.client_for).run(MODE_SINGLE, "问题", [A, B])
        assert result.error and recorder.asked == []

    def test_an_unknown_shape_is_refused_rather_than_treated_as_a_table(self) -> None:
        """拼错的 mode 走成圆桌，等于用户要点外卖、结果开了个会。"""
        recorder = ModeRecorder()
        result = Collaboration(recorder.client_for).run("debate", "问题", [A, B, C])
        assert "不认识的协作形态" in result.error
        assert recorder.asked == []

    def test_a_stop_between_two_workers_keeps_everything_already_paid_for(self) -> None:
        recorder = ModeRecorder()
        flips = {"n": 0}

        def stop_before_the_second_worker() -> bool:
            flips["n"] += 1
            return flips["n"] > 1

        result = Collaboration(recorder.client_for).run(
            MODE_BOSS, "问题", [A, B, C], should_stop=stop_before_the_second_worker
        )
        assert result.stopped is True
        assert len(recorder.asked) == 2, "拆单 + 第一家干活，之后就别再花钱了"
        assert len(result.speeches) == 2
        assert "停在 1/2 家" in result.record
