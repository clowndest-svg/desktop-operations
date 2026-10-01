"""Tests for the tool framework (jarvis.tools)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from jarvis.config.schema import ToolsSection
from jarvis.core.exceptions import ToolError
from jarvis.tools import (
    PERMISSION_SHELL,
    PERMISSION_WRITE,
    ExpressionError,
    RiskLevel,
    ToolPolicy,
    ToolRegistry,
    ToolService,
    ToolSpec,
    evaluate,
    evaluate_text,
    extract_expression,
    normalise_tool_name,
    object_schema,
    string_property,
    validate_arguments,
)
from jarvis.tools.builtins import build_builtin_tools
from jarvis.tools.builtins.web_tools import validate_url
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.tools.registry import RECENT_CALLS


def _section(**overrides: object) -> ToolsSection:
    raw: dict[str, object] = {
        "enabled": True,
        "confirm_dangerous": True,
        "allow_write": False,
        "allow_shell": False,
        "file_roots": [],
        "max_result_chars": 4000,
    }
    raw.update(overrides)
    return ToolsSection.from_mapping(raw)


def _registry(tmp_path: Path, **overrides: object) -> ToolRegistry:
    overrides.setdefault("file_roots", [str(tmp_path)])
    section = _section(**overrides)
    return ToolRegistry(lambda: section)


def _service(tmp_path: Path, **overrides: object) -> ToolService:
    overrides.setdefault("file_roots", [str(tmp_path)])
    section = _section(**overrides)
    return ToolService(
        lambda: section,
        monitor_factory=SystemMonitor,
        cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.log"),
    )


class TestSafeEval:
    def test_operator_precedence_is_real_python(self) -> None:
        """The reason for using ``ast`` instead of a regex: 2+3*4 is 14, not 20."""
        assert evaluate("2+3*4") == 14
        assert evaluate("(2+3)*4") == 20

    def test_supported_operators(self) -> None:
        assert evaluate("7/2") == 3.5
        assert evaluate("7//2") == 3
        assert evaluate("7%2") == 1
        assert evaluate("2**10") == 1024
        assert evaluate("-3+1") == -2

    def test_division_by_zero_is_a_readable_error(self) -> None:
        with pytest.raises(ExpressionError, match="除数不能为零"):
            evaluate("1/0")

    def test_absurd_exponent_is_refused(self) -> None:
        """``9**9**9`` is a denial of service, not a sum."""
        with pytest.raises(ExpressionError, match="指数过大"):
            evaluate("9**9999999")

    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('echo pwned')",
            "open('/etc/passwd').read()",
            "[x for x in range(3)]",
            "len('abc')",
            "a.b",
            "lambda: 1",
            "(1).__class__",
            "{'a': 1}",
        ],
    )
    def test_code_execution_is_refused(self, expression: str) -> None:
        """``eval()`` on model-authored text is remote code execution with extra
        steps; the AST whitelist is what makes each of these a parse error."""
        with pytest.raises(ExpressionError):
            evaluate(expression)

    def test_syntax_error_is_readable(self) -> None:
        with pytest.raises(ExpressionError, match="不是合法的算式"):
            evaluate("1+")

    def test_extract_expression_from_a_sentence(self) -> None:
        assert extract_expression("计算 1+1") == "1+1"

    def test_extract_returns_none_without_arithmetic(self) -> None:
        assert extract_expression("讲个笑话") is None

    def test_evaluate_text_reports_a_missing_expression(self) -> None:
        with pytest.raises(ExpressionError, match="未找到有效的算式"):
            evaluate_text("你好")

    def test_caret_is_treated_as_power(self) -> None:
        """Users type ``2^10``; the tool should not answer 8."""
        assert evaluate_text("2^10") == 1024


class TestArgumentValidation:
    _SPEC = ToolSpec(
        name="demo",
        description="d",
        parameters=object_schema(
            {
                "text": string_property("t"),
                "count": {"type": "integer"},
                "flag": {"type": "boolean"},
            },
            required=["text"],
        ),
    )

    def test_valid_arguments_pass(self) -> None:
        assert validate_arguments(self._SPEC, {"text": "a", "count": 2}) == []

    def test_missing_required_is_reported(self) -> None:
        assert "缺少必填参数 text" in validate_arguments(self._SPEC, {})[0]

    def test_unknown_argument_is_reported(self) -> None:
        assert "未知参数 nope" in validate_arguments(self._SPEC, {"text": "a", "nope": 1})[0]

    def test_wrong_type_is_reported(self) -> None:
        problems = validate_arguments(self._SPEC, {"text": 1})
        assert any("应为 string" in problem for problem in problems)

    def test_boolean_is_not_an_integer(self) -> None:
        """``True`` is an ``int`` in Python; a JSON Schema integer must not
        silently accept it."""
        problems = validate_arguments(self._SPEC, {"text": "a", "count": True})
        assert any("应为 integer" in problem for problem in problems)

    def test_schema_without_properties_accepts_anything(self) -> None:
        spec = ToolSpec(name="free", description="d", parameters={})
        assert validate_arguments(spec, {"whatever": 1}) == []


class TestToolNameNormalisation:
    def test_mcp_style_names_are_cleaned(self) -> None:
        assert normalise_tool_name("fs.read") == "fs_read"

    def test_chinese_names_are_cleaned(self) -> None:
        assert normalise_tool_name("读文件") == "tool"

    def test_already_valid_names_are_untouched(self) -> None:
        assert normalise_tool_name("read_file-2") == "read_file-2"

    def test_empty_input_falls_back(self) -> None:
        assert normalise_tool_name("   ") == "tool"


class TestToolPolicy:
    def test_shell_is_refused_while_disallowed(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(allow_shell=False))
        spec = ToolSpec(name="s", description="d", permissions=frozenset({PERMISSION_SHELL}))
        assert "allow_shell" in policy.check(spec, confirmed=True)

    def test_write_is_refused_while_disallowed(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(allow_write=False))
        spec = ToolSpec(name="w", description="d", permissions=frozenset({PERMISSION_WRITE}))
        assert "allow_write" in policy.check(spec, confirmed=True)

    def test_permission_wins_over_confirmation(self, tmp_path: Path) -> None:
        """Confirming a write while writes are off must still be refused: the
        switch says "this machine may write", the flag says "this call is fine"."""
        policy = ToolPolicy(lambda: _section(allow_write=False))
        spec = ToolSpec(name="w", description="d", permissions=frozenset({PERMISSION_WRITE}))
        assert policy.check(spec, confirmed=True) != ""

    def test_dangerous_needs_confirmation(self) -> None:
        policy = ToolPolicy(lambda: _section())
        spec = ToolSpec(name="d", description="d", risk=RiskLevel.DANGEROUS)
        assert "需要用户确认" in policy.check(spec, confirmed=False)
        assert policy.check(spec, confirmed=True) == ""

    def test_dangerous_is_allowed_when_confirmation_is_disabled(self) -> None:
        policy = ToolPolicy(lambda: _section(confirm_dangerous=False))
        spec = ToolSpec(name="d", description="d", risk=RiskLevel.DANGEROUS)
        assert policy.check(spec, confirmed=False) == ""

    def test_safe_tools_always_pass(self) -> None:
        policy = ToolPolicy(lambda: _section())
        assert policy.check(ToolSpec(name="s", description="d"), confirmed=False) == ""

    def test_file_roots_default_to_home(self) -> None:
        """An empty list must mean "the user's home", not "no restriction"."""
        policy = ToolPolicy(lambda: _section(file_roots=[]))
        assert policy.file_roots() == (Path.home().resolve(),)

    def test_resolve_path_inside_a_root(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        assert policy.resolve_path("a.txt") == (tmp_path / "a.txt").resolve()

    def test_relative_paths_ignore_the_working_directory(self, tmp_path: Path) -> None:
        """A desktop app launched from a shortcut can have any cwd at all, and
        resolving against it would put the file somewhere the user never meant.
        Resolving against a root also makes a relative path always in-bounds."""
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        resolved = policy.resolve_path("notes/today.md")
        assert resolved == (tmp_path / "notes" / "today.md").resolve()
        assert resolved.is_relative_to(tmp_path.resolve())

    def test_resolve_path_outside_a_root_is_refused(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        with pytest.raises(ToolError, match="不在允许的目录内"):
            policy.resolve_path(str(tmp_path.parent / "elsewhere.txt"))

    def test_traversal_cannot_escape(self, tmp_path: Path) -> None:
        """Resolution happens before the containment check; doing it the other
        way round lets ``<root>/../../`` pass and only then turn into something."""
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        with pytest.raises(ToolError):
            policy.resolve_path(str(tmp_path / ".." / ".." / "escape.txt"))

    def test_blank_path_is_refused(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        with pytest.raises(ToolError, match="不能为空"):
            policy.resolve_path("   ")

    def test_describe_reports_the_active_switches(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)], allow_write=True))
        described = policy.describe()
        assert described["allow_write"] is True
        assert described["file_roots"] == [str(tmp_path.resolve())]


class TestToolRegistry:
    def _spec(self, name: str = "demo", **kwargs: object) -> ToolSpec:
        return ToolSpec(name=name, description="d", **kwargs)  # type: ignore[arg-type]

    def test_register_and_invoke(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        registry.register(self._spec(), lambda arguments: f"got {arguments.get('x')}")
        result = registry.invoke("demo", {"x": 1})
        assert result.ok is True
        assert result.output == "got 1"
        assert result.tool == "demo"

    def test_unknown_tool_is_reported_not_raised(self, tmp_path: Path) -> None:
        result = _registry(tmp_path).invoke("nope")
        assert result.ok is False
        assert "未注册的工具" in result.error

    def test_policy_refusal_is_reported(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        registry.register(self._spec(permissions=frozenset({PERMISSION_SHELL})), lambda _: "ran")
        result = registry.invoke("demo", confirmed=True)
        assert result.ok is False
        assert "allow_shell" in result.error

    def test_bad_arguments_never_reach_the_handler(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        calls: list[Mapping[str, object]] = []
        registry.register(
            ToolSpec(
                name="needs_text",
                description="d",
                parameters=object_schema({"text": string_property("t")}, required=["text"]),
            ),
            lambda arguments: calls.append(arguments) or "ok",  # type: ignore[func-returns-value]
        )
        result = registry.invoke("needs_text", {})
        assert result.ok is False
        assert "缺少必填参数" in result.error
        assert calls == []

    def test_handler_exception_becomes_a_result(self, tmp_path: Path) -> None:
        """An exception reaching the voice path is an indistinguishable silence."""
        registry = _registry(tmp_path)

        def boom(_: Mapping[str, object]) -> str:
            raise ValueError("nope")

        registry.register(self._spec(), boom)
        result = registry.invoke("demo")
        assert result.ok is False
        assert "ValueError" in result.error

    def test_tool_error_message_is_preserved(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)

        def boom(_: Mapping[str, object]) -> str:
            raise ToolError("文件不存在")

        registry.register(self._spec(), boom)
        assert registry.invoke("demo").error == "文件不存在"

    def test_output_is_truncated_at_the_configured_limit(self, tmp_path: Path) -> None:
        """One listing of a large directory can otherwise consume the whole
        context window, leaving no room for the question."""
        registry = _registry(tmp_path, max_result_chars=10)
        registry.register(self._spec(), lambda _: "x" * 100)
        result = registry.invoke("demo")
        assert result.ok is True
        assert result.output.startswith("x" * 10)
        assert "已截断" in result.output

    def test_short_output_is_not_marked_truncated(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, max_result_chars=100)
        registry.register(self._spec(), lambda _: "short")
        assert registry.invoke("demo").output == "short"

    def test_same_source_re_registration_replaces(self, tmp_path: Path) -> None:
        """Makes a plugin reload idempotent."""
        registry = _registry(tmp_path)
        registry.register(self._spec(), lambda _: "first")
        registry.register(self._spec(), lambda _: "second")
        assert registry.invoke("demo").output == "second"

    def test_cross_source_collision_is_refused(self, tmp_path: Path) -> None:
        """Two packages both wanting ``read_file`` is a configuration problem;
        silently letting the later one win makes the tool list load-order
        dependent."""
        registry = _registry(tmp_path)
        registry.register(self._spec(), lambda _: "a")
        with pytest.raises(ToolError, match="工具名冲突"):
            registry.register(self._spec(source="plugin:other"), lambda _: "b")

    def test_empty_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="不能为空"):
            _registry(tmp_path).register(ToolSpec(name="  ", description="d"), lambda _: "")

    def test_unregister_and_unregister_source(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        registry.register(self._spec("a", source="plugin:p"), lambda _: "")
        registry.register(self._spec("b", source="plugin:p"), lambda _: "")
        registry.register(self._spec("c"), lambda _: "")
        assert registry.unregister("a") is True
        assert registry.unregister("a") is False
        assert registry.unregister_source("plugin:p") == 1
        assert registry.names() == ["c"]

    def test_unregister_unknown_source_returns_zero(self, tmp_path: Path) -> None:
        assert _registry(tmp_path).unregister_source("nope") == 0

    def test_clear(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        registry.register(self._spec(), lambda _: "")
        assert registry.clear() == 1
        assert registry.names() == []

    def test_specs_are_sorted_by_name(self, tmp_path: Path) -> None:
        """The tool list is part of the prompt; an unstable order changes the
        prefix on every boot and defeats provider-side prompt caching."""
        registry = _registry(tmp_path)
        for name in ("zeta", "alpha", "mid"):
            registry.register(self._spec(name), lambda _: "")
        assert registry.names() == ["alpha", "mid", "zeta"]

    def test_openai_schema_shape_hides_the_risk_level(self, tmp_path: Path) -> None:
        """The model must not be told which tools are dangerous, or it starts
        reasoning about the policy instead of the task."""
        registry = _registry(tmp_path)
        registry.register(self._spec(risk=RiskLevel.DANGEROUS), lambda _: "")
        payload = registry.to_openai_tools()[0]
        assert payload["type"] == "function"
        function = payload["function"]
        assert isinstance(function, Mapping)
        assert set(function) == {"name", "description", "parameters"}

    def test_stats_breaks_down_by_source_and_risk(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        registry.register(self._spec("a"), lambda _: "")
        registry.register(self._spec("b", risk=RiskLevel.DANGEROUS), lambda _: "")
        stats = registry.stats()
        assert stats["count"] == 2
        assert stats["by_risk"] == {"dangerous": 1, "safe": 1}
        assert stats["by_source"] == {"builtin": 2}

    def test_get_returns_none_for_unknown(self, tmp_path: Path) -> None:
        assert _registry(tmp_path).get("nope") is None


class TestBuiltinTools:
    def test_every_builtin_is_registered_and_well_formed(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        specs = service.specs()
        assert len(specs) >= 10
        for spec in specs:
            assert spec.name.isascii()
            assert spec.description
            assert spec.parameters.get("type") == "object"

    def test_start_is_idempotent(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        count = len(service.specs())
        service.start()
        assert len(service.specs()) == count

    def test_stop_clears_everything(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        service.stop()
        assert service.specs() == []
        assert service.running is False

    def test_disabled_config_registers_nothing_and_says_so(self, tmp_path: Path) -> None:
        service = _service(tmp_path, enabled=False)
        service.start()
        assert service.specs() == []
        assert service.invoke("current_time").ok is False
        assert "未启用" in service.invoke("current_time").error

    def test_current_time(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        output = service.invoke("current_time").output
        assert "本地时间" in output
        assert "UTC" in output

    def test_current_time_with_an_offset(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        assert "+08:00" in service.invoke("current_time", {"timezone_offset": "+08:00"}).output

    def test_current_time_rejects_a_bad_offset(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        assert "格式不对" in service.invoke("current_time", {"timezone_offset": "abc"}).output

    def test_calculate(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        assert service.invoke("calculate", {"expression": "计算 (12+8)*3/2"}).output.endswith("30")

    def test_calculate_integer_is_rendered_without_a_decimal(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        assert service.invoke("calculate", {"expression": "2+2"}).output.endswith("4")

    def test_calculate_refuses_injection(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("calculate", {"expression": "__import__('os').system('x')"})
        assert result.ok is False or "计算失败" in result.output

    def test_text_stats(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        output = service.invoke("text_stats", {"text": "abc\ndef", "top_chars": 1}).output
        assert "字符数：7" in output
        assert "行数：2" in output

    def test_list_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        service = _service(tmp_path)
        service.start()
        output = service.invoke("list_directory", {"path": str(tmp_path)}).output
        assert "a.txt" in output
        assert "sub/" in output

    def test_list_directory_with_a_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        (tmp_path / "b.log").write_text("x", encoding="utf-8")
        service = _service(tmp_path)
        service.start()
        output = service.invoke(
            "list_directory", {"path": str(tmp_path), "pattern": "*.log"}
        ).output
        assert "b.log" in output
        assert "a.txt" not in output

    def test_list_directory_on_a_missing_path(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("list_directory", {"path": str(tmp_path / "nope")})
        assert result.ok is False
        assert "目录不存在" in result.error

    def test_read_file(self, tmp_path: Path) -> None:
        target = tmp_path / "note.txt"
        target.write_text("你好世界", encoding="utf-8")
        service = _service(tmp_path)
        service.start()
        output = service.invoke("read_file", {"path": str(target)}).output
        assert "你好世界" in output

    def test_read_file_honours_max_chars(self, tmp_path: Path) -> None:
        target = tmp_path / "note.txt"
        target.write_text("abcdefghij", encoding="utf-8")
        service = _service(tmp_path)
        service.start()
        output = service.invoke("read_file", {"path": str(target), "max_chars": 3}).output
        assert "只读取了前 3 个字符" in output

    def test_read_file_refuses_a_directory(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("read_file", {"path": str(tmp_path)})
        assert result.ok is False
        assert "文件不存在" in result.error

    def test_read_outside_the_roots_is_refused(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("read_file", {"path": str(tmp_path.parent / "secret.txt")})
        assert result.ok is False
        assert "不在允许的目录内" in result.error

    def test_search_files(self, tmp_path: Path) -> None:
        (tmp_path / "deep").mkdir()
        (tmp_path / "deep" / "report.pdf").write_text("x", encoding="utf-8")
        service = _service(tmp_path)
        service.start()
        output = service.invoke("search_files", {"path": str(tmp_path), "pattern": "*.pdf"}).output
        assert "report.pdf" in output

    def test_search_files_with_no_hits(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        output = service.invoke("search_files", {"path": str(tmp_path), "pattern": "*.zzz"}).output
        assert "没有找到" in output

    def test_write_file_is_refused_by_default(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("write_file", {"path": str(tmp_path / "x.txt"), "content": "hi"})
        assert result.ok is False
        assert "allow_write" in result.error
        assert not (tmp_path / "x.txt").exists()

    def test_write_file_when_allowed(self, tmp_path: Path) -> None:
        service = _service(tmp_path, allow_write=True)
        service.start()
        target = tmp_path / "x.txt"
        assert service.invoke("write_file", {"path": str(target), "content": "hi"}).ok is True
        assert target.read_text(encoding="utf-8") == "hi"

    def test_write_file_append(self, tmp_path: Path) -> None:
        service = _service(tmp_path, allow_write=True)
        service.start()
        target = tmp_path / "x.txt"
        target.write_text("a", encoding="utf-8")
        service.invoke("write_file", {"path": str(target), "content": "b", "append": True})
        assert target.read_text(encoding="utf-8") == "ab"

    def test_delete_path_needs_confirmation(self, tmp_path: Path) -> None:
        """Two gates for delete: the write switch *and* a human's confirmation."""
        target = tmp_path / "x.txt"
        target.write_text("a", encoding="utf-8")
        service = _service(tmp_path, allow_write=True)
        service.start()
        result = service.invoke("delete_path", {"path": str(target)})
        assert result.ok is False
        assert "需要用户确认" in result.error
        assert target.exists()

    def test_delete_path_with_confirmation(self, tmp_path: Path) -> None:
        target = tmp_path / "x.txt"
        target.write_text("a", encoding="utf-8")
        service = _service(tmp_path, allow_write=True)
        service.start()
        result = service.invoke("delete_path", {"path": str(target)}, confirmed=True)
        assert result.ok is True
        assert not target.exists()

    def test_delete_refuses_a_non_empty_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "sub"
        target.mkdir()
        (target / "f").write_text("x", encoding="utf-8")
        service = _service(tmp_path, allow_write=True)
        service.start()
        result = service.invoke("delete_path", {"path": str(target)}, confirmed=True)
        assert result.ok is False
        assert "不是空的" in result.error

    def test_run_shell_is_not_advertised_by_default(self, tmp_path: Path) -> None:
        """Not "registered and refused" -- absent.

        The old behaviour was a tool that answered "tools.allow_shell 为 false" on
        every call, which measured as the assistant telling the operator it could
        not run commands: the model kept picking the one door that was locked and
        never tried ``run_powershell``, the one the 档位 button actually opens.
        """
        service = _service(tmp_path)
        service.start()
        assert "run_shell" not in {spec.name for spec in service.specs()}
        result = service.invoke("run_shell", {"command": "echo hi"}, confirmed=True)
        assert result.ok is False
        assert "未注册" in result.error

    def test_run_shell_when_allowed_and_confirmed(self, tmp_path: Path) -> None:
        service = _service(tmp_path, allow_shell=True)
        service.start()
        result = service.invoke("run_shell", {"command": "echo jarvis"}, confirmed=True)
        assert result.ok is True
        assert "jarvis" in result.output

    def test_run_shell_still_needs_confirmation(self, tmp_path: Path) -> None:
        service = _service(tmp_path, allow_shell=True)
        service.start()
        result = service.invoke("run_shell", {"command": "echo hi"})
        assert result.ok is False
        assert "需要用户确认" in result.error

    def test_system_report_degrades_without_psutil(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("system_report")
        # psutil is installed in this environment, so this asserts the shape
        # rather than the failure path: either a report or a readable reason.
        assert result.ok is True or "psutil" in result.error

    def test_fetch_url_validates_before_connecting(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        result = service.invoke("fetch_url", {"url": "not a url"})
        assert result.ok is False
        assert "协议头" in result.error


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://localhost/admin",
            "http://127.0.0.1:8080/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://myhost.local/x",
        ],
    )
    def test_dangerous_urls_are_refused(self, url: str) -> None:
        """A "fetch this page" tool must not become a filesystem reader or a
        port scanner for the user's own network."""
        with pytest.raises(ToolError):
            validate_url(url)

    def test_public_urls_pass(self) -> None:
        assert validate_url("https://example.com/page") == "https://example.com/page"

    def test_scheme_is_lowercased_in_the_result(self) -> None:
        assert validate_url("HTTPS://example.com") == "https://example.com"

    def test_blank_url_is_refused(self) -> None:
        with pytest.raises(ToolError, match="不能为空"):
            validate_url("   ")


class TestBuiltinAssembly:
    def test_every_tool_has_a_unique_name(self, tmp_path: Path) -> None:
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        tools = build_builtin_tools(
            policy,
            monitor_factory=SystemMonitor,
            cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.log"),
        )
        names = [spec.name for spec, _ in tools]
        assert len(names) == len(set(names))

    def test_tools_are_json_serialisable(self, tmp_path: Path) -> None:
        """The specs end up in a request body; a non-serialisable schema would
        fail at the worst possible moment."""
        policy = ToolPolicy(lambda: _section(file_roots=[str(tmp_path)]))
        tools = build_builtin_tools(
            policy,
            monitor_factory=SystemMonitor,
            cleaner_factory=lambda: DiskCleaner(tmp_path / "audit.log"),
        )
        payload = [spec.to_openai_schema() for spec, _ in tools]
        assert json.loads(json.dumps(payload)) == payload


class TestToolServiceStats:
    def test_stats_reports_policy_and_counts(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        stats = service.stats()
        assert stats["running"] is True
        assert stats["enabled"] is True
        policy = stats["policy"]
        assert isinstance(policy, Mapping)
        assert policy["allow_write"] is False

    def test_stats_works_before_start(self, tmp_path: Path) -> None:
        assert _service(tmp_path).stats()["running"] is False

    def test_external_registration_shares_the_registry(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.start()
        service.register(
            ToolSpec(name="external", description="d", source="plugin:x"), lambda _: "hi"
        )
        assert service.invoke("external").output == "hi"
        assert service.registry.get("external") is not None


class TestRecentCalls:
    """The ring the HUD's 最近动作 reads.

    The refusal being in there is the point. A gate that fired and said nothing to
    anybody is the same failure as a lock that was picked, from the operator's side:
    both look like "nothing happened".
    """

    def test_a_successful_call_is_remembered_with_one_line_of_its_output(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        registry.register(
            ToolSpec(name="demo", description="d"),
            lambda _arguments: "第一行\n第二行",
        )

        registry.invoke("demo")

        (entry,) = registry.recent()
        assert entry.tool == "demo"
        assert entry.ok is True
        assert entry.detail == "第一行"
        assert entry.at > 0

    def test_a_refusal_is_remembered_as_a_refusal(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)

        registry.invoke("nope")

        (entry,) = registry.recent()
        assert entry.ok is False
        assert "未注册的工具" in entry.detail

    def test_a_handler_that_raises_is_remembered_and_still_does_not_escape(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)

        def explode(_arguments: Mapping[str, object]) -> str:
            raise RuntimeError("boom")

        registry.register(ToolSpec(name="demo", description="d"), explode)

        assert registry.invoke("demo").ok is False
        assert registry.recent()[0].ok is False
        assert "RuntimeError" in registry.recent()[0].detail

    def test_the_ring_is_newest_first_and_bounded(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        registry.register(ToolSpec(name="demo", description="d"), lambda _arguments: "ok")

        for _ in range(RECENT_CALLS + 25):
            registry.invoke("demo")

        entries = registry.recent()
        assert len(entries) == RECENT_CALLS
        assert all(entry.tool == "demo" for entry in entries)

    def test_nothing_is_recorded_before_the_first_call(self, tmp_path: Path) -> None:
        assert _registry(tmp_path).recent() == ()
