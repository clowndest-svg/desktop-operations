"""Three ways to make several models answer one question, on one engine.

The shapes differ by exactly one thing: **who is allowed to see what.**

``table`` (圆桌轮流)
    Every seat reads everything said before it, once per round; the first seat merges.
    Discussion. Costs ``seats x (rounds + 1)`` and cannot be parallelised, because the
    next speaker is waiting for the previous one.

``boss`` (主管分发)
    One seat splits the question and collects; the others each see *only their own
    slice* and never learn the others exist. Divide and conquer -- cheaper
    (``seats + 1``, because the supervisor splits and collects but is handed no task of
    its own) and the only shape where a seat can be interrupted without invalidating
    another's work. It ignores ``rounds``: "send the same worker the same task again" is
    not a discussion, and pretending otherwise would be a knob that does nothing.

``vote`` (并行三答 + 互评投票)
    Everyone answers blind, then everyone reviews **anonymised** copies of all answers
    and picks one. The delivered answer is a model's own words, not a merge, which is
    the point: you are choosing between them. Costs ``2 x seats``.

Why anonymity is not optional: a model that knows which answer came from whom reliably
prefers its own voice, and a review round that can do that is not a vote -- it is a
roll call. Ballots here are shuffled per reviewer and labelled 答案一/二/三 with no
provider named. That defeats *most* of the self-preference, not all of it -- a model can
still recognise its own habits -- and :data:`LIMITS` says so in the record rather than
claiming a fairness this code cannot deliver.

Everything runs on the caller's thread, one seat at a time. Parallel seats would need a
streaming bubble per speaker (the page has one), and the money is identical either way:
this is a wall-clock decision wearing a UI constraint, so the UI constraint gets to be
the documented reason. :mod:`jarvis.app.turns` owns the thread, the task id and the stop
flag; a 停止 press between two seats costs one seat.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from typing import Any

from jarvis.app.round_table import (
    MAX_SEATS,
    RoundResult,
    RoundTable,
    Seat,
    Speech,
    _deliver,
    _render,
    speak_seat,
)

logger = logging.getLogger("jarvis.app.collaboration")

__all__ = [
    "COLLABORATION_MODES",
    "MODE_BOSS",
    "MODE_SINGLE",
    "MODE_TABLE",
    "MODE_VOTE",
    "Collaboration",
    "label_for",
    "requests_for",
]

MODE_SINGLE = "single"
"""One model, no table. The default, and the only mode that carries tools and pictures."""

MODE_TABLE = "table"
MODE_BOSS = "boss"
MODE_VOTE = "vote"

COLLABORATION_MODES: tuple[str, ...] = (MODE_SINGLE, MODE_TABLE, MODE_BOSS, MODE_VOTE)

_LABELS = {
    MODE_SINGLE: "单个模型",
    MODE_TABLE: "圆桌轮流",
    MODE_BOSS: "主管分发",
    MODE_VOTE: "并行三答·互评投票",
}

LIMITS = {
    MODE_BOSS: "主管模式不分轮次：同一份活再派一遍不是讨论。",
    MODE_VOTE: "评审认得出自己的文风，这里只保证认不出名字。",
}

_SPLIT_ASK = (
    "你是这次协作的主管，下面 {workers} 家会各领一件事，他们互相看不见对方。\n"
    "请把这个问题拆成 {workers} 件能独立完成的子任务，每家一件，要求：\n"
    "1) 严格输出 {workers} 行，每行写成「序号|要做的事」，序号从 1 开始；\n"
    "2) 除了这 {workers} 行不要说任何别的话，不要解释、不要标题、不要总结；\n"
    "3) 各件之间别重叠，也别合起来还缺一大块。\n\n问题：{question}"
)

_WORKER_ASK = (
    "有人在组织一次分工，你只领到自己这一件。别人在做什么你看不到，也不用猜。\n"
    "总问题是：{question}\n\n分给你的是：{task}\n\n"
    "只回答分给你的这一件，说人话，别写超过 200 字。"
)

_COLLECT_ASK = (
    "下面是各家的分工结果。请把它们合成一个回答：重合的别说两遍，"
    "互相矛盾的要指出来并给出你倾向的那个，缺的部分承认缺。\n"
    "直接给结论，不要点名感谢谁，也不要复述下面这份清单。\n\n{record}\n\n原问题：{question}"
)

_VOTE_ASK = (
    "下面 {count} 份回答来自同一个问题，作者是{anonymity}，你不需要知道是谁写的。\n"
    "挑出你认为最靠谱的一份：看它有没有真的回答问题、有没有编造、有没有把关键风险讲清楚。\n"
    "只输出一行，格式是「答案|理由」，例如「答案二|数据没瞎说还提了风险」。\n\n"
    "问题：{question}\n\n{answers}"
)

_BALLOT_NOT_VOTING = "这几份我都没法选"


def label_for(mode: str) -> str:
    """What the page calls a mode. Unknown values come back as-is so a typo is visible."""
    return _LABELS.get(mode, mode)


def requests_for(mode: str, seats: int, rounds: int = 2) -> int:
    """How many model calls this job is about to make. The page shows the number.

    Computed here rather than in the template so the price the operator sees cannot drift
    from the price the engine pays.
    """
    if seats <= 0:
        return 0
    if mode == MODE_BOSS:
        # 一次拆分 + (seats-1) 件活 + 一次汇总：主管自己不领活。
        return seats + 1
    if mode == MODE_VOTE:
        return 2 * seats
    if mode == MODE_TABLE:
        return seats * (max(1, rounds) + 1)
    return seats


class BossTable:
    """One seat splits, the rest work, the same seat collects."""

    def __init__(self, client_for: Callable[[str, str], Any]) -> None:
        self._client_for = client_for

    def run(
        self,
        question: str,
        seats: Sequence[Seat],
        *,
        system: str = "",
        history: Sequence[Any] = (),
        task_id: str = "",
        on_speech: Callable[[Speech], None] | None = None,
        on_floor: Callable[[Seat, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RoundResult:
        boss, workers = seats[0], list(seats[1:])
        stop = should_stop or _never
        split = speak_seat(
            self._client_for,
            boss,
            _SPLIT_ASK.format(workers=len(workers), question=question),
            round_index=0,
            system=system,
            history=history,
            task_id=task_id,
            stop=stop,
        )
        if split is None:
            return RoundResult(answer="", record="", error=f"{boss.label} 拆不了这道题（没答上来）")
        tasks, notes = _split_tasks(split.text, len(workers), question)
        speeches: list[Speech] = [Speech(round_index=0, seat=boss, text=split.text)]
        record_lines = [f"【主管 {boss.label} 拆分工】"] + [
            f"{index + 1}. {worker.label} ← {task}"
            for index, (worker, task) in enumerate(zip(workers, tasks, strict=True))
        ]
        for note in notes:
            record_lines.append(f"（{note}）")

        for index, (worker, task) in enumerate(zip(workers, tasks, strict=True), start=1):
            if stop():
                return _cut(index - 1, len(workers), speeches, record_lines)
            if on_floor is not None:
                _deliver(on_floor, worker, 1)
            speech = speak_seat(
                self._client_for,
                worker,
                _WORKER_ASK.format(question=question, task=task),
                round_index=1,
                system=system,
                history=history,
                task_id=task_id,
                on_delta=on_delta,
                stop=stop,
            )
            if speech is None:
                return RoundResult(
                    answer="",
                    record="\n".join(record_lines),
                    speeches=tuple(speeches),
                    error=f"{worker.label} 这一件没做完",
                )
            speeches.append(speech)
            record_lines.append(f"【{worker.label}】{speech.text}")
            if on_speech is not None:
                _deliver(on_speech, speech)

        if stop():
            return _cut(len(workers), len(workers), speeches, record_lines)
        merged = speak_seat(
            self._client_for,
            boss,
            _COLLECT_ASK.format(record="\n\n".join(record_lines), question=question),
            round_index=2,
            system=system,
            on_delta=on_delta,
            task_id=task_id,
            stop=stop,
        )
        if merged is None:
            last = speeches[-1]
            return RoundResult(
                answer=last.text,
                record="\n".join(record_lines),
                speeches=tuple(speeches),
                error="主管没能合起来，最后一家说的先原样给你",
            )
        return RoundResult(
            answer=merged.text,
            record="\n".join(record_lines),
            speeches=(*speeches, merged),
            rounds=2,
        )


class VoteTable:
    """Answer blind, review anonymously, count by rule."""

    def __init__(
        self, client_for: Callable[[str, str], Any], *, shuffle: random.Random | None = None
    ) -> None:
        self._client_for = client_for
        # Injectable so a test can pin one ballot order instead of praying at a seed.
        self._shuffle = shuffle or random.Random()

    def run(
        self,
        question: str,
        seats: Sequence[Seat],
        *,
        system: str = "",
        history: Sequence[Any] = (),
        task_id: str = "",
        on_speech: Callable[[Speech], None] | None = None,
        on_floor: Callable[[Seat, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RoundResult:
        stop = should_stop or _never
        answers: list[Speech] = []
        for seat in seats:
            if stop():
                return _cut(len(answers), len(seats), answers, ["【独立作答，还没评】"])
            if on_floor is not None:
                _deliver(on_floor, seat, 1)
            speech = speak_seat(
                self._client_for,
                seat,
                f"请回答这个问题，只说答案本身，别超过 300 字：\n\n{question}",
                round_index=1,
                system=system,
                history=history,
                task_id=task_id,
                on_delta=on_delta,
                stop=stop,
            )
            if speech is None:
                return RoundResult(
                    answer="",
                    record=_render(answers),
                    speeches=tuple(answers),
                    error=f"{seat.label} 没答上来，投票取消",
                )
            answers.append(speech)
            if on_speech is not None:
                _deliver(on_speech, speech)

        if len(answers) < 2:
            one = answers[0]
            return RoundResult(
                answer=one.text,
                record=f"【只有 {one.seat.label} 一家作答，没有可比的东西，这就是它的答案】",
                speeches=tuple(answers),
                rounds=1,
            )

        won_by = dict.fromkeys(range(len(answers)), 0)
        abstained = 0
        lines = ["【独立作答】"] + [
            f"{_word(index)}：{speech.text}" for index, speech in enumerate(answers)
        ]
        lines.append("")
        lines.append("【匿名互评】")
        for reviewer in answers:
            if stop():
                return _cut(len(answers), len(seats) * 2, answers, lines)
            if on_floor is not None:
                _deliver(on_floor, reviewer.seat, 2)
            order = list(range(len(answers)))
            self._shuffle.shuffle(order)
            sheet = "\n\n".join(
                f"{_word(slot)}：{answers[real].text}" for slot, real in enumerate(order)
            )
            verdict = speak_seat(
                self._client_for,
                reviewer.seat,
                _VOTE_ASK.format(
                    count=len(answers),
                    question=question,
                    answers=sheet,
                    anonymity="匿名给的，编号每次评审都重新排过",
                ),
                round_index=2,
                system=system,
                task_id=task_id,
                stop=stop,
            )
            if verdict is None:
                abstained += 1
                lines.append(f"{reviewer.seat.label}：弃权（它这次没答）")
                continue
            chosen = _read_ballot(verdict.text, order)
            if chosen is None:
                abstained += 1
                lines.append(f"{reviewer.seat.label}：弃权（它说的是「{verdict.text[:40]}」）")
                continue
            won_by[chosen] += 1
            lines.append(
                f"{reviewer.seat.label} 投给 {_word(order.index(chosen))}"
                f"（= {answers[chosen].seat.label}）：{verdict.text[:60]}"
            )

        top = max(won_by.values())
        winners = [index for index, tally in won_by.items() if tally == top]
        tie = len(winners) > 1
        pick = winners[0]
        lines.append("")
        lines.append(
            "【票面】"
            + "、".join(f"{answers[i].seat.label} {won_by[i]} 票" for i in range(len(answers)))
            + f"；弃权 {abstained}"
        )
        if tie:
            lines.append(
                f"（并列 {top} 票：{'、'.join(answers[i].seat.label for i in winners)}，"
                "按座位顺序取第一家。要真分出高下就再加一轮，那是另一次钱。）"
            )
        lines.append(LIMITS[MODE_VOTE])
        return RoundResult(
            answer=answers[pick].text,
            record="\n".join(lines),
            speeches=tuple(answers),
            rounds=2,
        )


class Collaboration:
    """The one door :class:`~jarvis.app.chat_service.ChatService` knocks on."""

    def __init__(
        self,
        client_for: Callable[[str, str], Any],
        *,
        max_rounds: int = 3,
    ) -> None:
        self._table = RoundTable(client_for, max_rounds=max_rounds)
        self._boss = BossTable(client_for)
        self._vote = VoteTable(client_for)

    def run(
        self,
        mode: str,
        question: str,
        seats: Sequence[Seat],
        *,
        rounds: int = 2,
        system: str = "",
        history: Sequence[Any] = (),
        task_id: str = "",
        on_speech: Callable[[Speech], None] | None = None,
        on_floor: Callable[[Seat, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RoundResult:
        chosen = [seat for seat in seats if seat is not None]
        if not chosen:
            return RoundResult(answer="", record="", error="没有选模型，协作开不了")
        if len(chosen) > MAX_SEATS:
            return RoundResult(
                answer="", record="", error=f"一桌最多 {MAX_SEATS} 家，多出来的没坐下"
            )
        if mode == MODE_SINGLE:
            return RoundResult(answer="", record="", error="单个模型不走协作这条路")
        if mode not in (MODE_TABLE, MODE_BOSS, MODE_VOTE):
            # Falling through to the table here would hand the operator a discussion when
            # they asked for a dispatch -- a typo in a mode string must not buy nine calls.
            return RoundResult(answer="", record="", error=f"不认识的协作形态：{mode}")
        if mode == MODE_BOSS and len(chosen) < 2:
            return RoundResult(
                answer="", record="", error="主管分发至少要两把椅子：一个拆，一个干活"
            )
        # Written out three times rather than packed into a **kwargs bag: each shape takes
        # a slightly different set (only the table has ``rounds``), and a dict that might
        # contain anything is exactly what the type checker cannot see through.
        if mode == MODE_BOSS:
            return self._boss.run(
                question,
                chosen,
                system=system,
                history=history,
                task_id=task_id,
                on_speech=on_speech,
                on_floor=on_floor,
                on_delta=on_delta,
                should_stop=should_stop,
            )
        if mode == MODE_VOTE:
            return self._vote.run(
                question,
                chosen,
                system=system,
                history=history,
                task_id=task_id,
                on_speech=on_speech,
                on_floor=on_floor,
                on_delta=on_delta,
                should_stop=should_stop,
            )
        return self._table.run(
            question,
            chosen,
            rounds=rounds,
            system=system,
            history=history,
            task_id=task_id,
            on_speech=on_speech,
            on_floor=on_floor,
            on_delta=on_delta,
            should_stop=should_stop,
        )


def _split_tasks(text: str, workers: int, question: str) -> tuple[list[str], list[str]]:
    """Read the supervisor's split into exactly ``workers`` tasks, announcing every repair.

    A model that hands back prose instead of ``序号|任务`` is the failure mode this shape
    was born with, and the two ways out are both bad: hard-fail loses the requests
    already paid for, silently guessing makes one worker do a task nobody read. So the
    fallback is *announced into the record* -- each unparsed seat gets the original
    question, and the operator sees that line before the answer.
    """
    tasks: list[str] = []
    for line in text.splitlines():
        head, sep, body = line.strip().partition("|")
        if not sep:
            continue
        number = head.strip().lstrip("答案第").strip()
        if not number.isdigit() or not body.strip():
            continue
        if int(number) == len(tasks) + 1:
            tasks.append(body.strip()[:400])
    notes: list[str] = []
    if not tasks:
        notes.append("主管没按「序号|要做的事」输出，这次每家都拿到的是原问题")
        return [question] * workers, notes
    if len(tasks) < workers:
        notes.append(f"主管只拆出 {len(tasks)} 件，剩下 {workers - len(tasks)} 家按原问题作答")
        tasks = tasks + [question] * (workers - len(tasks))
    elif len(tasks) > workers:
        notes.append(f"主管多拆了 {len(tasks) - workers} 件，多余的没派")
        tasks = tasks[:workers]
    return tasks, notes


def _word(slot: int) -> str:
    """答案一 / 答案二 ... 位置标签，永远不是名字。

    编号从 1 开始数，因为 ``slot`` 是下标而给人看的编号不是。上一条实现把下标直接印了
    出去：卷面上出现「答案〇」，提示语又写着"别选答案〇"，三份答案的票于是整体偏一家 ——
    数错票没人会看见，只会看见"选了谁"。
    """
    digits = "一二三四五六七八九"
    return f"答案{digits[slot] if slot < len(digits) else slot + 1}"


def _read_ballot(text: str, order: Sequence[int]) -> int | None:
    """Which answer this reviewer picked, or ``None`` for "that is not a ballot"."""
    if not text.strip():
        return None
    if _BALLOT_NOT_VOTING in text:
        return None
    for slot, real in enumerate(order):
        if _word(slot) in text:
            return real
    head = text.split("|", 1)[0].strip()
    if head.isdigit():
        index = int(head) - 1
        if 0 <= index < len(order):
            return order[index]
    return None


def _never() -> bool:
    return False


def _cut(done: int, planned: int, speeches: Sequence[Speech], lines: Sequence[str]) -> RoundResult:
    """What the operator gets when they stop mid-table."""
    logger.info("collaboration stopped after %d/%d seats", done, planned)
    last = speeches[-1] if speeches else None
    return RoundResult(
        answer=last.text if last else "",
        record="\n".join([*lines, f"（停在 {done}/{planned} 家）"]),
        speeches=tuple(speeches),
        stopped=True,
    )
