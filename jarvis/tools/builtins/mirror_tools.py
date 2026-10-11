"""The three tools that let her open the phone's screen on this computer.

Why these exist next to the panel rather than only in it: 「把手机投上来」 is a
sentence, and a model that has ``open_app`` but no mirror tool will happily launch
some third-party 投屏 app and report success. The panel door and the tool door have
to lead to the same ``ScreenMirrorService``, which is the one thing that owns the
child process.

The tools are registered only when the vendored runtime is actually in the package
(see :func:`build`), the same rule ``run_powershell`` and the mouse tools follow: a
capability the registry cannot reach must not be advertised, because the model will
otherwise promise the operator a window that can only ever fail to appear.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Protocol

from jarvis.tools.types import (
    RiskLevel,
    ToolHandler,
    ToolSpec,
    object_schema,
    string_property,
)

_DEVICES: Final = "mirror_devices"
_START: Final = "mirror_start"
_STOP: Final = "mirror_stop"
_SCREEN: Final = "phone_screen_read"


class MirrorRow(Protocol):
    """One ``adb`` device, as far as a tool may see it."""

    def to_dict(self) -> Mapping[str, object]: ...


class PhoneScreenAnswer(Protocol):
    """One read of the phone's screen. The text is the whole contract."""

    def to_text(self) -> str: ...


class PhoneScreenSurface(Protocol):
    """The reader behind :data:`_SCREEN`: ask the phone what is on its screen.

    Structural for the same reason as :class:`MirrorSurface` -- ``tools`` may not
    import ``app``, and a fourth copy of the wording would be wording that drifts.
    """

    def read(self, question: str = "") -> PhoneScreenAnswer: ...


class MirrorSurface(Protocol):
    """``ScreenMirrorService``: the four calls these tools need, and nothing else.

    Declared structurally because ``tools`` may not import ``app`` -- the same reason
    ``ShellGate`` and ``DesktopControl`` are protocols in this layer.
    """

    def available(self) -> bool: ...

    def status(self) -> Mapping[str, object]: ...

    def phones(self) -> tuple[Sequence[MirrorRow], str]: ...

    def start_mirror(self, serial: str = "") -> Mapping[str, object]: ...

    def stop_mirror(self) -> Mapping[str, object]: ...


_PHASE_WORDS: Final[dict[str, str]] = {
    "off": "没在投",
    "starting": "正在启动",
    "running": "正在投屏",
    "stopping": "正在关闭",
    "failed": "失败",
}


def _label_of(status: Mapping[str, object], key: str, wanted: str) -> str:
    """The Chinese label for one of the panel's two dropdowns, from the payload itself.

    Read out of ``status`` rather than imported from the app layer: ``tools`` may not
    import ``app``, and a second copy of the wording here is wording that drifts.
    """
    rows = status.get(key)
    if isinstance(rows, (list, tuple)):
        for row in rows:
            if isinstance(row, Mapping) and str(row.get("id")) == wanted:
                return str(row.get("label") or wanted)
    return wanted


def _describe_status(status: Mapping[str, object]) -> str:
    """One paragraph the model can quote, phase first, reason second."""
    phase = str(status.get("phase") or "off")
    word = _PHASE_WORDS.get(phase, phase)
    serial = str(status.get("serial") or "")
    tier = str(status.get("tier") or "")
    mode = str(status.get("mode") or "")
    seconds = status.get("seconds") or 0
    line = f"投屏状态：{word}"
    if serial:
        line += f"（{serial}，{tier} 档，已经 {seconds} 秒）"
    elif tier:
        line += f"（当前档位：{tier}）"
    if mode:
        line += f"；方式：{_label_of(status, 'modes', mode)}"
    detail = str(status.get("detail") or "")
    if detail:
        line += f"\n说明：{detail}"
    if not status.get("available", True):
        line += "\n注意：这个包里没带 scrcpy 运行时，投屏开不了。"
    tail = status.get("tail")
    if isinstance(tail, (list, tuple)) and tail and phase in ("running", "failed"):
        line += "\n最后几行输出：" + " | ".join(str(item) for item in tail[-3:])
    return line


def _devices(mirror: MirrorSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        rows, error = mirror.phones()
        if error:
            return f"问不到手机列表：{error}"
        status = mirror.status()
        mode = str(status.get("mode") or "")
        by_mode = f"（当前方式：{_label_of(status, 'modes', mode)}）" if mode else ""
        if not rows:
            return (
                "现在没有能投屏的手机" + by_mode.rstrip("）") + "）。"
                "要么用 USB 线连一台并在手机上点「允许 USB 调试」，"
                "要么先在投屏面板里走一次「无线调试」配对"
            )
        lines = [f"连着 {len(rows)} 台手机{by_mode}："]
        for row in rows:
            device = row.to_dict()
            mark = "可投屏" if device.get("ready") else "投不了"
            wireless = "无线" if device.get("wireless") else "USB"
            text = f"- {device.get('label')}（{device.get('serial')}，{wireless}，{mark}）"
            raw = device.get("channels")
            rest = [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []
            others = [item for item in rest if item != str(device.get("serial"))]
            if others:
                text += f"（同一台还有 {len(others)} 个通道：{'、'.join(others)}）"
            hint = str(device.get("hint") or "")
            lines.append(f"{text}{f'：{hint}' if hint else ''}")
        return "\n".join(lines)

    return handler


def _start(mirror: MirrorSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        serial = str(arguments.get("serial") or "").strip()
        return _describe_status(mirror.start_mirror(serial))

    return handler


def _stop(mirror: MirrorSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        return _describe_status(mirror.stop_mirror())

    return handler


_SCREEN_SPEC: Final[ToolSpec] = ToolSpec(
    name=_SCREEN,
    description=(
        "读手机上现在这一屏：当前是哪个 App、屏幕上有哪些文字。"
        "用户问「我手机上现在是什么」「手机上那个验证码多少」「你看看我手机」时用它。"
        "只读，不会改手机上的任何东西；投屏没开着或 adb 里连着两台时会回答读不到哪台。"
        "回答里每一行都写了它是从哪来的（手机自报的无障碍树 / 本地 OCR / 看图模型）。"
    ),
    parameters=object_schema(
        {"question": string_property("用户具体想从这一屏知道什么，可留空；留空就通读一遍")}
    ),
    risk=RiskLevel.CAUTION,
)

_SPECS: Final[tuple[tuple[ToolSpec, str], ...]] = (
    (
        ToolSpec(
            name=_DEVICES,
            description=(
                "列出现在能投屏的手机（连了线或连了同一个 WiFi 并已配对的安卓设备），"
                "以及每一台是不是可用。只读。用户问「手机连上了吗」「能看到我手机吗」"
                "或要开始投屏而不确定是哪一台时先用它。"
            ),
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.SAFE,
        ),
        _DEVICES,
    ),
    (
        ToolSpec(
            name=_START,
            description=(
                "把手机屏幕投到这台电脑上，开一个独立的投屏窗口。"
                "只有一台手机时可以不说 serial；连着多台必须指定，否则会拒绝并要求先问"
                "「mirror_devices」。画质档位用面板里记住的那一档。"
                "用户说「投屏」「把手机画面弄到电脑上来」时用这一条，"
                "不要改用 open_app 去找别的投屏软件。"
            ),
            parameters=object_schema(
                {"serial": string_property("要投的那台设备的序列号或 IP:端口，可留空")}
            ),
            risk=RiskLevel.CAUTION,
        ),
        _START,
    ),
    (
        ToolSpec(
            name=_STOP,
            description="关掉正在开的投屏窗口。没在投的时候调用也安全，只会回答没在投。",
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.CAUTION,
        ),
        _STOP,
    ),
)


def _screen(phone: PhoneScreenSurface) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        question = str(arguments.get("question") or "").strip()
        return phone.read(question).to_text()

    return handler


def build(
    mirror: MirrorSurface, phone: PhoneScreenSurface | None = None
) -> list[tuple[ToolSpec, ToolHandler]]:
    """The mirror tools, or nothing when this build cannot run scrcpy at all.

    Checked here rather than inside each handler so a model with no runtime never
    sees the names: 「小夜帮你打开投屏了」 followed by no window is the exact failure
    this codebase has been asked repeatedly to stop producing.
    """
    if not mirror.available():
        return []
    handlers: dict[str, ToolHandler] = {
        _DEVICES: _devices(mirror),
        _START: _start(mirror),
        _STOP: _stop(mirror),
        _SCREEN: _screen(phone) if phone is not None else _no_phone_reader(),
    }
    tools = [(spec, handlers[key]) for spec, key in _SPECS]
    if phone is None:
        # 没有读屏的那一层（命令行入口没接手机），就不广告这一条：模型会答应得极其自然，
        # 然后每次都说"我看不清"。没有运行时的投屏工具是同一个道理。
        return tools
    return [*tools, (_SCREEN_SPEC, handlers[_SCREEN])]


def _no_phone_reader() -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        del arguments
        return "这个进程没接手机读屏。"

    return handler


__all__ = ["MirrorSurface", "PhoneScreenSurface", "build"]
