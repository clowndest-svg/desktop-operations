"""Several models answering one task together, at the same table.

The shape came from the operator: 圆桌轮流，每一轮三家都看同一份记录发一次言，最后一轮合并.
So this is not an agent graph, not a debate with fixed roles, and not a router that picks
one winner -- it is a repeated read-the-transcript-say-something loop over an ordered list
of models the operator chose, followed by one seat that has to commit to an answer.

Why the record is a text block rather than replayed assistant messages: an ``assistant``
turn from deepseek in the history reads as *this model's own earlier words* to qwen, which
then will not contradict it. Written as a transcript inside a user turn, it is clearly
somebody else's opinion, and disagreeing is the point of inviting three models.

Everything here runs on the caller's thread. :mod:`jarvis.app.turns` owns the thread, the
task id and the stop flag; this module only ever asks "may I speak next", so a 停止 press
between two seats costs one seat, not the whole table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.core.exceptions import JarvisError
from jarvis.llm.client import LlmClient, StreamingClient
from jarvis.llm.types import ChatMessage, GenerationOptions, Role

logger = logging.getLogger("jarvis.app.round_table")

__all__ = ["MAX_ROUNDS", "MAX_SEATS", "RoundResult", "RoundTable", "Seat", "Speech"]

MAX_SEATS = 4
"""How many models may sit at one table.

A cap because the cost is seats x (rounds + 1) requests and the operator is spending their
own key: three models over two rounds is already nine calls, and "let me just add everyone"
should not be able to produce forty without saying so.
"""

MAX_ROUNDS = 3
"""Discussion rounds before the merge. See :data:`MAX_SEATS` for why this is bounded."""

DEFAULT_ROUNDS = 2

_MODERATOR_ASK = (
    "下面是这次圆桌讨论的完整记录。请把各家说过的话合并成一个最终回答："
    "保留真正有用的部分，指出仍然存在的分歧，不要点名感谢谁，也不要复述记录本身。"
    "直接给结论。\n\n{record}\n\n问题：{question}"
)

_SEAT_ASK = (
    "这是一场圆桌讨论，一共 {seats} 家，每人每轮发一次言。{round_line}\n\n"
    "{record}\n\n问题：{question}\n\n"
    "现在轮到你。只说你自己要补充的，看着上面的记录来：同意的就别重复，"
    "不同意的要指出哪里不对。不要复述别人的话，控制在 200 字以内。"
)

_RECORD_EMPTY = "（还没有人发言，你是第一个。）"


@dataclass(frozen=True, slots=True)
class Seat:
    """One chair: a provider and a model, in the order the operator picked them."""

    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else self.model

    @staticmethod
    def of(value: Any) -> Seat | None:
        """Read one seat out of whatever the page sent, or ``None`` if it is not a seat."""
        if isinstance(value, Seat):
            return value
        if not isinstance(value, dict):
            return None
        provider = str(value.get("provider") or "").strip()
        model = str(value.get("model") or "").strip()
        if not model and not provider:
            return None
        return Seat(provider=provider[:60], model=model[:150])


@dataclass(frozen=True, slots=True)
class Speech:
    """One turn of the floor.

    Carries its own token counts because the bill for a table is the interesting number:
    a task answered by three models over two rounds is nine requests, and "这花了多少"
    can only be answered if each seat's share survived.
    """

    round_index: int
    seat: Seat
    text: str
    reasoning: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def as_record_line(self) -> str:
        return f"【第 {self.round_index} 轮 · {self.seat.label}】{self.text}"

    def to_dict(self) -> dict[str, object]:
        return {
            "round": self.round_index,
            "provider": self.seat.provider,
            "model": self.seat.model,
            "text": self.text,
            "reasoning": self.reasoning,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True, slots=True)
class RoundResult:
    """What came back from the table: the merged answer, and everything said to get there."""

    answer: str
    record: str
    """Every speech, in order, as text. Shown under a fold; not replayed as the answer."""

    speeches: tuple[Speech, ...] = ()
    rounds: int = 0
    stopped: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def prompt_tokens(self) -> int | None:
        return _sum_or_none(speech.prompt_tokens for speech in self.speeches)

    @property
    def completion_tokens(self) -> int | None:
        return _sum_or_none(speech.completion_tokens for speech in self.speeches)

    def to_dict(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "record": self.record,
            "speeches": [speech.to_dict() for speech in self.speeches],
            "rounds": self.rounds,
            "stopped": self.stopped,
            "error": self.error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class RoundTable:
    """Runs the discussion. Stateless between calls: the record lives in the arguments."""

    def __init__(
        self,
        client_for: Callable[[str, str], LlmClient],
        *,
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        self._client_for = client_for
        self._max_rounds = max(1, min(max_rounds, MAX_ROUNDS))

    def run(
        self,
        question: str,
        seats: Sequence[Seat],
        *,
        rounds: int = DEFAULT_ROUNDS,
        on_speech: Callable[[Speech], None] | None = None,
        on_floor: Callable[[Seat, int], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        task_id: str = "",
        system: str = "",
        history: Sequence[ChatMessage] = (),
    ) -> RoundResult:
        """Seat everyone, talk, merge. Never raises: a table that throws loses the room.

        ``on_delta`` is handed the whole partial of the speech being given, as everywhere
        else in this project -- the UI pump is latest-wins, so a diff would leave a hole.
        """
        chosen = [seat for seat in seats if seat is not None]
        if not chosen:
            return RoundResult(answer="", record="", error="没有选模型，圆桌开不了")
        if len(chosen) > MAX_SEATS:
            return RoundResult(
                answer="",
                record="",
                error=f"一桌最多 {MAX_SEATS} 家，多出来的没坐下",
            )
        rounds = max(1, min(int(rounds or DEFAULT_ROUNDS), self._max_rounds))
        stop = should_stop or _never
        speeches: list[Speech] = []
        floor = 0

        for round_index in range(1, rounds + 1):
            for seat in chosen:
                if stop():
                    return _stopped(chosen, speeches, rounds, floor)
                if on_floor is not None:
                    _deliver(on_floor, seat, round_index)
                speech = self._speak(
                    question,
                    seat,
                    round_index=round_index,
                    seats=chosen,
                    speeches=speeches,
                    task_id=task_id,
                    on_delta=on_delta,
                    stop=stop,
                    system=system,
                    history=history,
                )
                if speech is None:
                    return RoundResult(
                        answer="",
                        record=_render(speeches),
                        speeches=tuple(speeches),
                        rounds=round_index,
                        error=f"{seat.label} 这一轮没答上来",
                    )
                speeches.append(speech)
                floor += 1
                if on_speech is not None:
                    _deliver(on_speech, speech)

        if stop():
            return _stopped(chosen, speeches, rounds, floor)
        # The first seat merges: the operator ordered the list, and whoever they put first
        # is who they trust to commit.
        if on_floor is not None:
            _deliver(on_floor, chosen[0], rounds + 1)
        merged = self._speak(
            question,
            chosen[0],
            round_index=rounds + 1,
            seats=chosen,
            speeches=speeches,
            task_id=task_id,
            on_delta=on_delta,
            stop=stop,
            moderator=True,
            system=system,
            history=history,
        )
        if merged is None:
            # The discussion is real even if the merge fails: falling back to the last
            # word spoken is better than losing everything after paying for all of it.
            last = speeches[-1] if speeches else None
            return RoundResult(
                answer=last.text if last else "",
                record=_render(speeches),
                speeches=tuple(speeches),
                rounds=rounds,
                stopped=False,
                error="合并这一步没成，最后一段发言就是现在的结论",
            )
        return RoundResult(
            answer=merged.text,
            record=_render(speeches),
            # The merge is billed but not quoted: it is a speech for the token count, and
            # putting it in the record would show the operator their own conclusion listed
            # back to them as one opinion among three.
            speeches=(*speeches, merged),
            rounds=rounds + 1,
        )

    def _speak(
        self,
        question: str,
        seat: Seat,
        *,
        round_index: int,
        seats: Sequence[Seat],
        speeches: Sequence[Speech],
        task_id: str,
        on_delta: Callable[[str], None] | None,
        stop: Callable[[], bool],
        moderator: bool = False,
        system: str = "",
        history: Sequence[ChatMessage] = (),
    ) -> Speech | None:
        """One seat, one turn of the floor. ``None`` when that seat could not answer."""
        prompt = _MODERATOR_ASK if moderator else _SEAT_ASK
        ask = prompt.format(
            record=_render(speeches),
            question=question,
            seats=len(seats),
            round_line=(
                f"现在是第 {round_index} 轮。" if not moderator else "这是最后一轮：合并出结论。"
            ),
        )
        return speak_seat(
            self._client_for,
            seat,
            ask,
            round_index=round_index,
            system=system,
            history=history,
            task_id=task_id,
            on_delta=on_delta,
            stop=stop,
        )


def speak_seat(
    client_for: Callable[[str, str], LlmClient],
    seat: Seat,
    ask: str,
    *,
    round_index: int,
    system: str = "",
    history: Sequence[ChatMessage] = (),
    task_id: str = "",
    on_delta: Callable[[str], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> Speech | None:
    """Ask one model one thing, and fold the answer into a :class:`Speech`.

    The whole reason this is a free function is that all three collaboration shapes ask a
    seat the same question in different words -- "who could not answer" and "what did it
    cost" must not be decided three times differently. ``None`` is the only failure it
    reports; every reason (a client that cannot be built, a provider that threw) is logged
    here, because the caller has no way to say *why* a chair came back empty.
    """
    waiter = stop if stop is not None else _never
    try:
        client = client_for(seat.provider, seat.model)
    except JarvisError as exc:
        logger.error("seat %s cannot be opened: %s", seat.label, exc)
        return None
    except Exception:
        logger.exception("seat %s could not be built into a client", seat.label)
        return None
    messages: list[ChatMessage] = []
    if system:
        messages.append(ChatMessage(role=Role.SYSTEM, content=system))
    messages.extend(history)
    messages.append(ChatMessage.user(ask))
    options = GenerationOptions(task_id=task_id)
    try:
        reply = _ask(client, messages, options, on_delta=on_delta, stop=waiter)
    except JarvisError as exc:
        logger.error("seat %s failed: %s", seat.label, exc)
        return None
    except Exception:
        logger.exception("seat %s blew up", seat.label)
        return None
    usage = reply.usage
    return Speech(
        round_index=round_index,
        seat=seat,
        text=(reply.content or "").strip(),
        reasoning=reply.reasoning,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
    )


def _ask(
    client: LlmClient,
    messages: list[ChatMessage],
    options: GenerationOptions,
    *,
    on_delta: Callable[[str], None] | None,
    stop: Callable[[], bool],
) -> Any:
    """Stream when there is somebody watching the floor, otherwise ask plainly.

    A speech is shown as it is written, which is also the only way a stop lands
    between two sentences instead of after the whole one.
    """
    if on_delta is None or not isinstance(client, StreamingClient):
        return client.complete(messages, options=options)
    return client.complete_stream(messages, options=options, on_text=on_delta, should_stop=stop)


def _render(speeches: Sequence[Speech]) -> str:
    if not speeches:
        return _RECORD_EMPTY
    return "\n\n".join(speech.as_record_line() for speech in speeches)


def _stopped(
    seats: Sequence[Seat],
    speeches: list[Speech],
    rounds: int,
    floor: int,
) -> RoundResult:
    """What the table has when the operator cuts it off.

    The speeches so far are kept rather than dropped: nine requests' worth of reasoning
    that vanishes on 停止 reads as nothing happened, and the record is the one part of
    the turn nobody else has.
    """
    logger.info(
        "round table stopped after %d speech(es) of %d seats x %d round(s)",
        floor,
        len(seats),
        rounds,
    )
    last = speeches[-1] if speeches else None
    return RoundResult(
        answer=last.text if last else "",
        record=_render(speeches),
        speeches=tuple(speeches),
        rounds=rounds,
        stopped=True,
    )


def _deliver(listener: Callable[..., None], *args: Any) -> None:
    """One broken listener must not silence the table.

    The page's callbacks draw things, and a widget that throws on one speech would
    otherwise take the discussion down with it -- after the requests were already paid
    for. The exception is logged, because "the panel never updated" has to be traceable
    to the panel.
    """
    try:
        listener(*args)
    except Exception:
        logger.exception("round table listener raised; ignoring")


def _sum_or_none(values: Any) -> int | None:
    seen = [value for value in values if value is not None]
    return sum(seen) if seen else None


def _never() -> bool:
    return False


def refuse_pictures(seat: Seat, images: Sequence[str]) -> str:
    """The sentence shown when a picture is pasted into a collaborative question.

    A table of three models where two of them silently never saw the screenshot is the
    exact failure this project keeps being told about: visible on the page, invisible in
    the answer. Refusing is honest, costs nothing, and says what to do instead.
    """
    if not images:
        return ""
    return f"协作模式不带图（{seat.label} 这边没法保证每一家都看得见）；要问这张图，换回单个模型。"
