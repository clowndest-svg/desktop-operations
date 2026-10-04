"""The desktop-control tools: what the model can ask for, and what it cannot.

The interesting assertions here are not "a click reaches the controller" -- the
service already tests that. They are the ones about the *seam*: that a policy veto
comes back as readable text instead of an exception, that dry-run never says it did
something, and that ``confirmed`` is not a parameter any tool accepts. That last one
is the whole safety model; a single tool that took a ``confirmed`` argument would let
a prompt talk the assistant into clicking past the gate, and it would do so happily,
because passing ``true`` is what a model does when a boolean looks required.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, cast

from jarvis.computer.service import ComputerService
from jarvis.computer.types import PlannedAction
from jarvis.config.schema import ComputerSection
from jarvis.core.exceptions import DangerousOperationRejectedError
from jarvis.tools.builtins import build_builtin_tools
from jarvis.tools.builtins.computer_tools import build
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.types import RiskLevel, ToolHandler, ToolSpec


def _section(**overrides: object) -> ComputerSection:
    data: dict[str, object] = {
        "enabled": True,
        "dry_run": True,
        "allow_mouse": True,
        "allow_keyboard": True,
        "allow_typing": True,
        "confirm_dangerous": False,
    }
    data.update(overrides)
    return ComputerSection.from_mapping(data)


class _Recorder:
    """A controller that remembers, and never moves a real cursor."""

    name = "recorder"

    def __init__(self) -> None:
        self.actions: list[PlannedAction] = []

    def execute(self, action: PlannedAction) -> None:
        self.actions.append(action)


def _tools(**overrides: object) -> tuple[dict[str, ToolHandler], dict[str, ToolSpec], _Recorder]:
    recorder = _Recorder()
    service = ComputerService(lambda: _section(**overrides), controller=recorder)
    service.start()
    pairs = build(lambda: service)
    handlers = {spec.name: handler for spec, handler in pairs}
    specs = {spec.name: spec for spec, _ in pairs}
    return handlers, specs, recorder


def _call(handlers: dict[str, ToolHandler], name: str, **arguments: Any) -> str:
    return handlers[name](arguments)


class TestRegistration:
    def test_every_tool_has_a_spec_and_a_handler(self) -> None:
        handlers, specs, _ = _tools()

        assert set(handlers) == set(specs)
        assert {"computer_status", "mouse_move", "mouse_click", "mouse_scroll"} <= set(handlers)

    def test_no_tool_accepts_a_confirmation_flag(self) -> None:
        """The gate only exists if the caller cannot satisfy it themselves."""
        _handlers, specs, _ = _tools()

        for name, spec in specs.items():
            properties = spec.parameters.get("properties") or {}
            assert isinstance(properties, Mapping)
            assert "confirmed" not in properties, f"{name} would let a prompt clear the gate"
            assert "confirm" not in properties, f"{name} would let a prompt clear the gate"

    def test_the_registry_omits_desktop_tools_without_a_service(self) -> None:
        """Advertising a capability nothing can perform is how a model promises the
        operator something that will only ever be refused."""
        policy = ToolPolicy(_tools_policy_section)

        names = {
            spec.name
            for spec, _ in build_builtin_tools(
                policy,
                monitor_factory=_never_called("telemetry"),
                cleaner_factory=_never_called("the cleaner"),
            )
        }

        assert "mouse_click" not in names
        assert "system_report" in names


class TestRefusalsComeBackAsText:
    def test_a_disabled_service_explains_the_switch(self) -> None:
        handlers, _specs, recorder = _tools(enabled=False)

        answer = _call(handlers, "mouse_click", x=10, y=20)

        assert "computer.enabled" in answer
        assert recorder.actions == []

    def test_a_disabled_mouse_explains_its_own_switch(self) -> None:
        handlers, _specs, recorder = _tools(allow_mouse=False)

        answer = _call(handlers, "mouse_click", x=10, y=20)

        assert "allow_mouse" in answer
        assert recorder.actions == []

    def test_bad_coordinates_are_described_not_raised(self) -> None:
        handlers, _specs, _recorder = _tools()

        assert "参数不对" in _call(handlers, "mouse_move", x="left", y=2)

    def test_an_unknown_mouse_button_is_rejected_before_the_controller(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=False)

        answer = _call(handlers, "mouse_click", x=1, y=1, button="side")

        assert "side" in answer
        assert recorder.actions == []

    def test_a_typing_dump_is_refused_on_length_alone(self) -> None:
        """Long text means the model is pasting context into a field, and every
        character of it would land somewhere real."""
        handlers, _specs, recorder = _tools(dry_run=False)

        answer = _call(handlers, "type_text", text="字" * 900)

        assert "400" in answer
        assert recorder.actions == []


class TestExecution:
    def test_dry_run_never_claims_the_cursor_moved(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=True)

        answer = _call(handlers, "mouse_click", x=40, y=50)

        assert "未真实执行" in answer
        assert recorder.actions == [], "dry run must not reach the controller at all"

    def test_a_real_run_reports_what_happened(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=False)

        answer = _call(handlers, "mouse_move", x=40, y=50)

        assert answer.startswith("已执行")
        assert recorder.actions[0].kind.value == "move"

    def test_key_press_is_the_workhorse_without_vision(self) -> None:
        handlers, _specs, recorder = _tools(dry_run=False)

        _call(handlers, "key_press", key="win")

        assert recorder.actions[0].params["key"] == "win"

    def test_status_reads_the_service_not_the_configuration(self) -> None:
        handlers, _specs, _recorder = _tools()

        answer = _call(handlers, "computer_status")

        assert "演练模式" in answer


def _never_called(label: str) -> Callable[[], Any]:
    """A factory the registry must not reach, because reaching it is the bug."""

    def build() -> Any:
        raise AssertionError(f"{label} is not part of this test")

    return build


def _tools_policy_section() -> Any:
    from jarvis.config.schema import ToolsSection

    # Built field by field: the policy now reads ``allow_shell`` to decide what to
    # advertise, and ``from_mapping`` leaves the keys it was not given as ``None``,
    # which the schema rightly refuses to treat as a boolean.
    return ToolsSection(
        enabled=True,
        confirm_dangerous=True,
        allow_write=False,
        allow_shell=False,
        file_roots=(),
        max_result_chars=10_000,
    )


class TestDeclaredRisk:
    """A click is not a read. The registry's own vocabulary has to say so.

    ``--tools`` prints this column to an operator deciding what to leave switched
    on, and ``SAFE`` on ``mouse_click`` is the kind of label that makes the listing
    worse than nothing.
    """

    def test_every_mutating_tool_declares_a_side_effect(self) -> None:
        _handlers, specs, _ = _tools()

        read_only = {"computer_status"}
        for name, spec in specs.items():
            if name in read_only:
                assert spec.risk is RiskLevel.SAFE, f"{name} reads; it should not be flagged"
                continue
            assert spec.risk is RiskLevel.CAUTION, f"{name} moves something real"

    def test_nothing_claims_to_need_a_confirmation_it_cannot_take(self) -> None:
        """DANGEROUS would route through the registry's ``confirmed`` argument, which
        no tool here may accept -- so the label would promise a gate that never fires
        and silently make the whole feature unreachable from a chat turn."""
        _handlers, specs, _ = _tools()

        assert all(spec.risk is not RiskLevel.DANGEROUS for spec in specs.values())


class _FakeApp:
    """One search hit, shaped like ``jarvis.computer.app_launcher.AppCandidate``."""

    def __init__(
        self,
        name: str,
        source: str = "开始菜单",
        target: str = r"C:\ProgramData\Start Menu\Programs\X\X.lnk",
        *,
        running: bool = False,
        pid: int = 0,
        note: str = "",
    ) -> None:
        self.name = name
        self.source = source
        self.target = target
        self.running = running
        self.pid = pid
        self.note = note


class _AppService:
    """A desktop-control service that answers the app search from a script."""

    name = "fake-apps"

    def __init__(self, hits: list[Any], *, sources: tuple[str, ...] = ("开始菜单",)) -> None:
        self._hits = hits
        self._sources = sources
        self.launched: list[tuple[str, str, int]] = []
        self.queries: list[str] = []

    def find_apps(self, query: str) -> Any:
        self.queries.append(query)
        return tuple(self._hits), self._sources

    def launch(self, target: str, *, label: str = "", pid: int = 0) -> Any:
        self.launched.append((target, label, pid))
        return SimpleNamespace(ok=True, executed=True, detail=f"启动「{label}」（{target}）")

    def stats(self) -> dict[str, object]:
        return {
            "enabled": True,
            "dry_run": False,
            "controller": self.name,
            "executed": 0,
            "failed": 0,
        }


class TestOpenApp:
    r"""按名字打开程序 —— 这一条是用户拿真话问出来的。

    他让助手"打开微信给老妈发句你好"，助手自己写 PowerShell 去 ``Program Files\Tencent``
    找 ``WeChat.exe``、去 App Paths 找 ``WeChat.exe``，然后回答"这台电脑上找不到微信" ——
    而它当时正跑在 ``D:\RuanJian\微信\Weixin\Weixin.exe``，开始菜单里还有它的快捷方式。
    查找不该靠模型自由发挥，答案里必须写着它是在哪儿找到的。
    """

    def _handlers(self, service: _AppService) -> dict[str, ToolHandler]:
        return {spec.name: handler for spec, handler in build(cast(Any, lambda: service))}

    def test_the_top_hit_is_launched_and_the_source_is_named(self) -> None:
        service = _AppService(
            [
                _FakeApp("微信", "开始菜单", r"C:\ProgramData\...\微信.lnk"),
                _FakeApp("微信输入法", "用户程序目录", r"C:\Users\x\...\WeixinIME.exe"),
            ]
        )

        answer = self._handlers(service)["open_app"]({"name": "微信"})

        assert service.launched == [(r"C:\ProgramData\...\微信.lnk", "微信", 0)]
        assert "开始菜单" in answer
        assert "微信输入法" in answer, "另外那些候选要说出来，否则她只能靠猜"

    def test_an_already_running_app_is_brought_forward_instead_of_started_again(self) -> None:
        service = _AppService(
            [
                _FakeApp(
                    "Weixin.exe",
                    "正在运行",
                    r"D:\RuanJian\微信\Weixin\Weixin.exe",
                    running=True,
                    pid=13532,
                )
            ]
        )

        self._handlers(service)["open_app"]({"name": "微信"})

        assert service.launched == [(r"D:\RuanJian\微信\Weixin\Weixin.exe", "Weixin.exe", 13532)]

    def test_not_finding_it_lists_where_it_looked(self) -> None:
        service = _AppService([], sources=("正在运行", "开始菜单", "注册表 App Paths"))

        answer = self._handlers(service)["open_app"]({"name": "微信"})

        assert "没找到" in answer
        assert "开始菜单" in answer and "注册表 App Paths" in answer
        assert "完整路径" in answer
        assert service.launched == []

    def test_an_empty_name_is_refused_before_any_search(self) -> None:
        service = _AppService([_FakeApp("微信")])

        answer = self._handlers(service)["open_app"]({"name": "   "})

        assert "不能为空" in answer
        assert service.queries == []

    def test_a_policy_refusal_comes_back_as_text(self) -> None:
        class _Refusing(_AppService):
            def launch(self, target: str, *, label: str = "", pid: int = 0) -> Any:
                raise DangerousOperationRejectedError(
                    "桌面控制未启用；请在配置中设置 computer.enabled=true"
                )

        service = _Refusing([_FakeApp("微信")])

        answer = self._handlers(service)["open_app"]({"name": "微信"})

        assert "被安全策略拒绝" in answer and "computer.enabled" in answer
