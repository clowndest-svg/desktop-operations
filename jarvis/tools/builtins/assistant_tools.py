"""The assistant's own tools: say it, remember it, and look both up again.

Why these are here and not in ``app``
-------------------------------------
The model can only reach what the registry advertises, so "念给我听" and "十分钟后
提醒我" have to be tools. But ``tools`` sits below ``app`` in the dependency table
and that rule is machine-checked, so nothing here imports a reminder, an announcer,
a memory or a knowledge base: all four arrive as the narrow protocols below, and the
composition root hands in the real objects. That is the same seam ``shell_tools``
uses for ``ShellGate`` and ``computer_tools`` uses for ``DesktopControl``.

Why every one of these is SAFE
------------------------------
None of them touch the machine. They write a row in the assistant's own database and
make it talk. The dangerous capabilities are on the other side of a tier the operator
has to raise by hand; putting these behind one would only teach the operator to raise
it for no reason.

Why the two search tools exist at all
-------------------------------------
Memory and knowledge used to be only ever *pushed* at the model: the chat service
stuffed the profile block into the system prompt and ran one retrieval over the raw
question. That works until the question is "接着上次那个说" — the retrieval term is
then that sentence, and nothing matches. These two tools are the pull half: the model
may re-search with a keyword it picked itself, mid-conversation, as many times as it
needs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from jarvis.core.exceptions import ToolError
from jarvis.tools.types import (
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)

logger = logging.getLogger("jarvis.tools.builtins.assistant_tools")


class Speaker(Protocol):
    """The announcer: one sentence in, a line about what happened out."""

    def announce(self, text: str, *, title: str = "小夜") -> str: ...


class Reminders(Protocol):
    """The reminder service, as far as a tool is concerned."""

    def add(self, text: str, when_text: str = "") -> object: ...

    # Sequence, not list: a service returning the concrete row type must still
    # satisfy this, and list is invariant where Sequence is covariant.
    def all_reminders(self) -> Sequence[object]: ...

    def cancel(self, key: str) -> str: ...


class Dictifiable(Protocol):
    """Something a tool can render without knowing which service made it.

    The memory hit and the memory record are different dataclasses in ``memory``,
    which this package may not import; both carry ``to_dict()``, and that is the
    only shape a tool needs in order to print a number without inventing one.
    """

    def to_dict(self) -> dict[str, object]: ...


class MemorySurface(Protocol):
    """Long-term memory, as far as a tool is allowed to see it.

    ``running`` is part of the surface because it decides whether the tools get
    registered at all: an unstarted memory service raises on every call, and an
    advertised capability that can only ever answer "记忆服务未启动" is worse than
    the model not knowing it exists.
    """

    @property
    def running(self) -> bool: ...

    def recall(self, query: str, *, top_k: int = ...) -> Sequence[Dictifiable]: ...

    def remember(self, content: str, *, kind: str = ..., source: str = ...) -> Dictifiable: ...


class EvidenceHit(Protocol):
    """One retrieved chunk, with the provenance that makes it citable."""

    @property
    def text(self) -> str: ...

    def citation(self) -> str: ...


class KnowledgeSurface(Protocol):
    """The knowledge base, narrowed to the one call a search tool makes.

    Gated on ``enabled`` as well as ``running``: a disabled base returns an empty
    list from every query, so the tool would report "没找到" about a corpus the
    operator switched off -- a confident wrong answer, which is the one thing a
    retrieval tool must never produce.
    """

    @property
    def running(self) -> bool: ...

    @property
    def enabled(self) -> bool: ...

    def retrieve(self, query: str, *, top_k: int = ...) -> Sequence[EvidenceHit]: ...


def _spec(
    name: str,
    description: str,
    parameters: Mapping[str, object] | None = None,
    *,
    required: Sequence[str] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=object_schema(dict(parameters or {}), required=required),
        risk=RiskLevel.SAFE,
    )


_SPEAK = _spec(
    "speak",
    "用语音朗读一段话。用户说「念给我听」「读一下」、或者回答本来就是一句要听到的话时使用。"
    "参数 text 是要念出来的内容（标点不会被念出来，数字会被念成中文）。"
    "title 只有弹托盘气泡时看得到，提醒类的播报写「提醒」。",
    {
        "text": string_property("要朗读的文本"),
        "title": string_property("托盘气泡的标题，默认「小夜」"),
    },
)

_ADD = _spec(
    "add_reminder",
    "设置一条提醒。用户说「十分钟后提醒我喝水」「明天九点半提醒我开会」时使用。"
    "when 支持「N 分钟/小时/天后」「今天/明天/后天 + 时间」「19:40」「2026-10-03 08:00」；"
    "听不懂的时间会直接被拒绝，不要换个写法硬试。",
    {
        "text": string_property("到点要提醒的内容，一句话"),
        "when": string_property("什么时候提醒，中文时间短语或 ISO 时间"),
    },
)

_LIST = _spec(
    "list_reminders",
    "列出还没到的提醒。用户问「我还有什么提醒」「下一个提醒是什么时候」时使用。只读。",
)

_CANCEL = _spec(
    "cancel_reminder",
    "取消一条提醒。可以给它的内容里的词（「取消喝水那条」）或列表里的 job_id。"
    "如果有多条都像，会拒绝并要求说得更具体，不会替你猜。",
    {"key": string_property("提醒内容里的关键词，或 job_id")},
)

_MEMORY_SEARCH = _spec(
    "memory_search",
    "查小夜自己的长期记忆（关于这个用户的事实、偏好、发生过的事）。"
    "系统提示里那份「长期记忆」只是最常用的几条，用户提到小夜没主动想起来的东西时"
    "（「上次那个」「我跟你说过」「还记得吗」）用这句话里的关键词来查，不要直接说没印象。"
    "查询词用具体的名词，不要把整句原话（尤其是「接着上次那个说」这种指代）当查询词。",
    {
        "query": string_property("要查的内容里的关键词，比如「JDK 版本」「我弟弟的名字」"),
        "limit": integer_property("最多返回几条，默认 5", minimum=1),
    },
    required=("query",),
)

_REMEMBER = _spec(
    "remember",
    "把一件事写进长期记忆。用户明确说「记住这个」「以后都这样」时使用，"
    "或者你自己发现一条值得长期保留的偏好。"
    "content 用第三人称、能独立看懂的一句话（「用户希望回答简短」），不要写带「这个」「他」的原文，"
    "因为以后它会被单独塞进提示词，没有上下文可指。同一句话重复记不会变大，只会更新。"
    "不要记临时性的、一轮就过期的东西。",
    {
        "content": string_property("要长期记住的一句话，第三人称"),
        "kind": string_property(
            "fact（关于用户的稳定事实，默认）、preference（用户希望你怎么回答）、"
            "episode（发生过的一件事）",
        ),
    },
    required=("content",),
)

_KNOWLEDGE_SEARCH = _spec(
    "knowledge_search",
    "在用户导入的资料（知识库）里检索原文片段，返回带出处的片段正文。"
    "本轮自动带进来的资料片段可能没有命中，或者用户问的是另一件事时用这个再查一次；"
    "回答时引用编号要对得上返回的出处。查询词用资料里可能出现的说法，不要照抄口语指代。"
    "返回空说明资料里没有，就照实说没找到，不要用通用知识冒充资料里的内容。",
    {
        "query": string_property("要检索的关键词或问题"),
        "limit": integer_property("最多返回几个片段，默认 5", minimum=1),
    },
    required=("query",),
)

_MEMORY_KINDS: frozenset[str] = frozenset({"fact", "preference", "episode"})
"""The kinds a tool may write.

``summary`` and ``task`` are left out: a summary is a compressed stretch of
conversation that only the memory service can produce honestly, and an open task
belongs to the reminder scheduler, not to a note in the profile.
"""


def _text_of(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _limit_of(arguments: Mapping[str, object]) -> int:
    """A requested page size, or ``0`` meaning "use the service's own default".

    Deliberately not a second copy of the default: ``max_recall`` and
    ``knowledge.top_k`` already say how much to hand back, and a tool-level 5 would
    silently override an operator who set that config to 10.
    """
    value = arguments.get("limit")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def build(
    *,
    speaker: Speaker | None = None,
    reminders: Reminders | None = None,
    memory: MemorySurface | None = None,
    knowledge: KnowledgeSurface | None = None,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Assemble the tools whose backing service exists.

    A missing backing object means the tool is left out rather than registered to
    fail at call time: an advertised capability that answers "not configured" every
    single time is worse than the model not knowing it could have tried. The
    memory and knowledge tools additionally check ``running`` / ``enabled``, because
    those are the two states where a search would come back with an empty list and
    the model would report "you never told me" about something it did.
    """
    tools: list[tuple[ToolSpec, ToolHandler]] = []
    if speaker is not None:
        tools.append((_SPEAK, _make_speak(speaker)))
    if reminders is not None:
        tools.extend(_reminder_tools(reminders))
    if memory is not None and memory.running:
        tools.extend(_memory_tools(memory))
    if knowledge is not None and knowledge.running and knowledge.enabled:
        tools.extend(_knowledge_tools(knowledge))
    return tools


def _make_speak(speaker: Speaker) -> ToolHandler:
    def speak(arguments: Mapping[str, object]) -> str:
        text = _text_of(arguments, "text")
        if not text:
            return "没有要念的内容"
        title = _text_of(arguments, "title")
        return speaker.announce(text, title=title) if title else speaker.announce(text)

    return speak


def _reminder_tools(reminders: Reminders) -> list[tuple[ToolSpec, ToolHandler]]:
    def add(arguments: Mapping[str, object]) -> str:
        text = _text_of(arguments, "text")
        when = _text_of(arguments, "when")
        try:
            made = reminders.add(text, when)
        except Exception as exc:
            return f"设置失败：{exc}"
        return f"已设置提醒：{_field(made, 'text')}，{_field(made, 'when')}"

    def show(arguments: Mapping[str, object]) -> str:
        rows = reminders.all_reminders()
        waiting = [row for row in rows if _field(row, "enabled") is not False]
        if not waiting:
            return "现在没有待办的提醒"
        lines = [f"{_field(row, 'when')} {_field(row, 'text')}" for row in waiting[:20]]
        return "待办提醒：\n" + "\n".join(lines)

    def cancel(arguments: Mapping[str, object]) -> str:
        key = _text_of(arguments, "key") or _text_of(arguments, "text")
        try:
            return f"已取消：{reminders.cancel(key)}"
        except Exception as exc:
            return f"取消失败：{exc}"

    return [(_ADD, add), (_LIST, show), (_CANCEL, cancel)]


_KIND_LABELS: Mapping[str, str] = {
    "fact": "事实",
    "preference": "偏好",
    "episode": "经历",
    "summary": "摘要",
    "task": "待办",
}


def _memory_tools(memory: MemorySurface) -> list[tuple[ToolSpec, ToolHandler]]:
    def search(arguments: Mapping[str, object]) -> str:
        query = _text_of(arguments, "query")
        if not query:
            return "没有要查的关键词"
        hits = memory.recall(query, top_k=_limit_of(arguments))
        if not hits:
            return (
                f"记忆里没有找到和「{query}」相关的条目。"
                "换一个更具体的关键词再查一次，还是没有就照实说没印象。"
            )
        lines: list[str] = []
        for index, hit in enumerate(hits, start=1):
            row = hit.to_dict()
            kind = str(row.get("kind") or "")
            label = _KIND_LABELS.get(kind, kind)
            paths = [str(path) for path in _as_sequence(row.get("matched_by"))]
            tail = f"（相关度 {row.get('score')}"
            if paths:
                tail += f"，命中路径：{'、'.join(paths)}"
            lines.append(f"{index}. [{label}] {row.get('content')} {tail}）")
        return f"关于「{query}」的记忆 {len(lines)} 条：\n" + "\n".join(lines)

    def remember(arguments: Mapping[str, object]) -> str:
        content = _text_of(arguments, "content")
        if not content:
            return "没有要记的内容"
        kind = (_text_of(arguments, "kind") or "fact").lower()
        if kind not in _MEMORY_KINDS:
            # Raised rather than returned: the registry records it as a failed call, so
            # "she tried to remember something and was refused" is visible in 最近动作
            # instead of looking like a memory that was written.
            raise ToolError(
                f"kind 只能是 {'、'.join(sorted(_MEMORY_KINDS))} 之一，收到的是「{kind}」"
            )
        try:
            row = memory.remember(content, kind=kind, source="model").to_dict()
        except Exception as exc:
            return f"没能写进记忆：{exc}"
        label = _KIND_LABELS.get(kind, kind)
        stored = str(row.get("content") or content)
        if str(row.get("created_at") or "") != str(row.get("updated_at") or ""):
            return f"这条早就记住了（{label}）：{stored}"
        return f"已记住（{label}）：{stored}"

    return [(_MEMORY_SEARCH, search), (_REMEMBER, remember)]


def _knowledge_tools(knowledge: KnowledgeSurface) -> list[tuple[ToolSpec, ToolHandler]]:
    def search(arguments: Mapping[str, object]) -> str:
        query = _text_of(arguments, "query")
        if not query:
            return "没有要检索的问题"
        hits = knowledge.retrieve(query, top_k=_limit_of(arguments))
        if not hits:
            return (
                f"资料库里没有和「{query}」对得上的片段。"
                "换成资料里可能出现的说法再查一次；还是没有就说没找到，不要用通用知识冒充资料内容。"
            )
        blocks = [
            f"[{index}] 来源：{hit.citation()}\n{hit.text}" for index, hit in enumerate(hits, 1)
        ]
        return f"资料检索「{query}」命中 {len(blocks)} 段：\n\n" + "\n\n".join(blocks)

    return [(_KNOWLEDGE_SEARCH, search)]


def _as_sequence(value: object) -> Sequence[object]:
    """A list-or-None field read as a sequence, so a caller never iterates ``None``."""
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _field(row: object, name: str) -> object:
    """Read one field out of a value object or its dict view.

    The reminder row is a dataclass here and a plain dict after ``to_dict()``, and a
    tool that has to know which one it holds is a tool that will be wrong the first
    time someone changes the service's return type.
    """
    if isinstance(row, dict):
        return row.get(name, "")
    return getattr(row, name, "")


__all__ = ["build"]
