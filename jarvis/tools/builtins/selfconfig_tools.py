"""Tools that let her adjust herself -- inside a whitelist, and visibly.

Three things she could not do at all before: search her own past conversations, read and
move a handful of her own settings, and change the voice she speaks with. The page has
had all three for a while; the model could only say "你去设置里改一下".

Why a whitelist rather than the settings panel's whole surface
-------------------------------------------------------------
The panel edits endpoints, model rows and API keys. A model that can rewrite a base URL
can point the assistant at anything, and an operator reading 「我把地址改好了」 has no way
to tell that from an exfiltration setup. So the writable set is these five keys and no
others, chosen because each one is about *how she answers* rather than *what she can
reach*.

Ranges are not repeated here. ``SettingsService`` owns the bounds and answers with a
per-field problem string; a second copy of "64 to 16000" in a tool is a bound that drifts.

Why every write announces itself
-------------------------------
:meth:`SettingsService.apply` is called with ``by="model"``, which tags each key that
actually moved as 小夜改的 on the panel. That is what makes this set of tools acceptable
at all: the change is not just reversible, it is attributable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from jarvis.tools.types import (
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)


class SettingsMirror(Protocol):
    """``SettingsService`` as far as these tools may see it."""

    def snapshot(self) -> Mapping[str, object]: ...

    def apply(self, patch: dict[str, Any], *, by: str = ...) -> Mapping[str, object]: ...


class VoiceChanger(Protocol):
    """``VoicePicker``: the current voice, and the ones that exist."""

    def voices(self) -> Mapping[str, object]: ...

    def pick(self, voice_id: object) -> Mapping[str, object]: ...


class ConversationSearch(Protocol):
    """``TranscriptService.search``, the one read these tools need of it."""

    def search(self, query: str, *, limit: int = ...) -> Sequence[Mapping[str, object]]: ...


WRITABLE_SETTINGS: dict[str, str] = {
    "thinking_enabled": "bool",
    "thinking_budget": "int",
    "history_turns": "int",
    "auto_speak_typed": "bool",
    "telemetry_interval_ms": "int",
}
"""The keys she may move, and the JSON type each takes.

Deliberately a *readable* module constant: the whitelist is the argument for letting her
edit herself at all, so it has to be one list a person can read in one screen rather
than five checks scattered through handlers.
"""

READABLE_SETTINGS: tuple[str, ...] = (
    "provider",
    "model",
    "thinking_enabled",
    "thinking_budget",
    "history_turns",
    "auto_speak_typed",
    "telemetry_interval_ms",
    "api_key_set",
    "ai_edited",
)
"""What ``settings_read`` reports.

``api_key_variable`` is a name and ``api_key_set`` is a boolean, so nothing secret
crosses the bridge -- and the key itself never appears anywhere in this payload.
"""

DEFAULT_SEARCH_LIMIT = 8


def _spec(
    name: str,
    description: str,
    parameters: Mapping[str, object] | None = None,
    *,
    required: Sequence[str] = (),
    risk: RiskLevel = RiskLevel.SAFE,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=object_schema(dict(parameters or {}), required=required),
        risk=risk,
    )


_CHAT_SEARCH = _spec(
    "chat_search",
    "在以前聊过的内容里搜一句话，跨所有会话，返回出处（会话标题和时间）。"
    "用户说「上次我们说到」「之前你答过什么」「你还记得我问过」而记忆里没有时用它——"
    "记忆存的是结论，原话在这里。只读。",
    {
        "query": string_property("要搜的词或短句，用用户原话里出现过的说法"),
        "limit": integer_property("最多返回几条，默认 8", minimum=1),
    },
    required=("query",),
)

_SETTINGS_READ = _spec(
    "settings_read",
    "读小夜自己的配置：当前模型、是否显示思考过程、思考预算、上下文轮数、"
    "打字问题要不要念出来、遥测轮询间隔，以及哪些项是小夜改的。只读。"
    "改之前先用它看清当前值，改完才能说清「从 A 改成 B」。",
)

_SETTINGS_APPLY = _spec(
    "settings_apply",
    "改小夜自己的一个设置。只能改这几个键："
    + "、".join(sorted(WRITABLE_SETTINGS))
    + "。别的一律拒绝，模型地址、provider、密钥、权限档位都改不了。"
    "改完必须回话说明把哪一项从什么改成了什么，界面上那一行会标「小夜改的」。"
    "值超出范围会带原因退回来，不要换个写法硬试。",
    {
        "key": string_property("要改的键名，必须是上面那几个之一"),
        "value": {"description": "新值。布尔键传 true/false，整数键传整数（思考预算、轮数、毫秒）"},
    },
    required=("key", "value"),
    risk=RiskLevel.CAUTION,
)

_VOICE_PICK = _spec(
    "voice_pick",
    "换小夜说话用的音色，下一句生效。name 必须是 voice 列表里的名字——"
    "不认识的名字会退回来并列出可选音色，照那个列表挑，不要猜。"
    "当前音色可以从 settings_read 之外用本工具的返回里看到。",
    {"name": string_property("音色名，例如「晓晓」或引擎要求的标识原文")},
    required=("name",),
    risk=RiskLevel.CAUTION,
)


def build(
    *,
    settings: SettingsMirror | None = None,
    voices: VoiceChanger | None = None,
    conversations: ConversationSearch | None = None,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """Assemble the tools whose backing service is actually wired.

    Each one is dropped when its object is missing: the CLI has no settings panel and no
    voice stack, and a tool that answers "没有这个设置服务" to every call teaches the
    model that it can configure itself right up to the moment it tries.
    """
    tools: list[tuple[ToolSpec, ToolHandler]] = []
    if conversations is not None:
        tools.append((_CHAT_SEARCH, _chat_search(conversations)))
    if settings is not None:
        tools.append((_SETTINGS_READ, _settings_read(settings)))
        tools.append((_SETTINGS_APPLY, _settings_apply(settings)))
    if voices is not None:
        tools.append((_VOICE_PICK, _voice_pick(voices)))
    return tools


def _chat_search(conversations: ConversationSearch) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("query")
        query = raw.strip() if isinstance(raw, str) else ""
        if not query:
            return "没有要搜的词"
        limit = arguments.get("limit")
        count = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 0
        hits = conversations.search(query, limit=count or DEFAULT_SEARCH_LIMIT)
        if not hits:
            return f"以前聊过的话里没有「{query}」。可以换个更短的说法再搜一次。"
        lines = [f"聊过的内容里找到 {len(hits)} 处「{query}」："]
        for index, row in enumerate(hits, start=1):
            who = "用户" if row.get("role") == "user" else "小夜"
            title = str(row.get("title") or "（没有标题的会话）")
            lines.append(f"{index}. [{title} · {row.get('at')}] {who}：{row.get('excerpt')}")
        return "\n".join(lines)

    return handler


def _settings_read(settings: SettingsMirror) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        snapshot = settings.snapshot()
        lines = ["小夜当前的设置："]
        for key in READABLE_SETTINGS:
            if key not in snapshot:
                continue
            lines.append(f"  {key} = {_render(snapshot[key])}")
        return "\n".join(lines)

    return handler


def _settings_apply(settings: SettingsMirror) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        key = arguments.get("key")
        if not isinstance(key, str) or key.strip() not in WRITABLE_SETTINGS:
            return (
                f"这个键不在她能改的范围里：{key!r}。能改的只有 "
                f"{'、'.join(sorted(WRITABLE_SETTINGS))}"
            )
        name = key.strip()
        value = arguments.get("value")
        expected = WRITABLE_SETTINGS[name]
        if expected == "bool" and not isinstance(value, bool):
            return f"{name} 要的是 true 或 false，收到的是 {value!r}"
        if expected == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{name} 要的是整数，收到的是 {value!r}"
        before = settings.snapshot().get(name, "（读不到原值）")
        outcome = settings.apply({name: value}, by="model")
        problems = outcome.get("problems")
        if isinstance(problems, Mapping) and problems.get(name):
            return f"没有改成：{problems[name]}"
        after = outcome.get(name, value)
        return (
            f"已把 {name} 从 {_render(before)} 改成 {_render(after)}（界面上会标「小夜改的」）。"
            "请把这句话说给用户听，让他知道是哪一项变了。"
        )

    return handler


def _voice_pick(voices: VoiceChanger) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("name")
        wanted = raw.strip() if isinstance(raw, str) else ""
        if not wanted:
            return "没有要给她的音色名"
        before = str(voices.voices().get("current") or "")
        outcome = voices.pick(wanted)
        current = str(outcome.get("current") or "")
        if current and current == wanted:
            previous = f"之前是「{before}」，" if before and before != wanted else ""
            return f"已换成「{wanted}」说话。{previous}下一句就用它，不用重启。"
        reason = str(outcome.get("error") or "没换成功")
        return f"没换成「{wanted}」：{reason}。可选音色：{_choice_list(outcome)}"

    return handler


def _choice_list(payload: Mapping[str, object]) -> str:
    """The voice names out of whatever ``voices()``/``pick()`` answered with.

    ``label`` first because that is the readable name, ``id`` because that is what
    ``voice_pick`` has to be sent -- a list of only labels would send her guessing.
    """
    choices = payload.get("choices")
    if not isinstance(choices, Sequence):
        return "（读不到音色列表）"
    names: list[str] = []
    for entry in choices:
        if isinstance(entry, Mapping):
            label = str(entry.get("label") or "")
            ident = str(entry.get("id") or "")
            names.append(f"{label}（传 {ident}）" if label and ident else (ident or label))
        else:
            names.append(str(entry))
    return "、".join(names) or "（空）"


def _render(value: object) -> str:
    """A setting value the model can read back verbatim.

    Lists are joined because ``ai_edited`` is one and a Python repr would hand the model
    brackets and quotes to paraphrase.
    """
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value) or "（无）"
    return str(value)


__all__ = ["WRITABLE_SETTINGS", "build"]
