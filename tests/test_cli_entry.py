"""Tests for the command-line entry point's helpers (jarvis/__main__.py).

``__main__`` is the composition root and mostly untestable without starting the
whole application, but two of its pieces are pure logic with real failure modes:
the logging settings factory, and the argument surface itself.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis.__main__ import (
    _logging_settings_factory,
    _run_cli,
    _voice_stack_builder,
    _VoiceStack,
    build_arg_parser,
)
from jarvis.app.chat_service import ChatService
from jarvis.app.voice_graph import ChatGraph
from jarvis.app.wake_greeting import WakeGreeter
from jarvis.config import AppPaths
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig
from jarvis.core.exceptions import JarvisError
from tests._fakes import FakeLlmClient


class _ConfigStub:
    """The two attributes the factory reads, and nothing else."""

    def __init__(self, level: str) -> None:
        raw = load_defaults()
        section = raw["logging"]
        assert isinstance(section, dict)
        logging_section = {**section, "level": level}
        raw["logging"] = logging_section
        self.config: AppConfig = AppConfig.from_mapping(raw)
        self.paths = AppPaths.from_data_dir(Path("E:/tmp/jarvis-test"))


class TestLoggingSettingsFactory:
    def test_verbose_forces_debug(self) -> None:
        """``--desktop -v`` used to open DevTools but leave the log at INFO —
        backwards for someone trying to find out why the window is blank, since
        the interesting records are the DEBUG ones."""
        factory = _logging_settings_factory(_ConfigStub("INFO"), verbose=True)  # type: ignore[arg-type]
        assert factory().section.level == "DEBUG"

    def test_without_verbose_the_configured_level_wins(self) -> None:
        factory = _logging_settings_factory(_ConfigStub("WARNING"), verbose=False)  # type: ignore[arg-type]
        assert factory().section.level == "WARNING"

    def test_verbose_is_not_retroactive(self) -> None:
        """The provider is read at start time, so a second call must still see
        the override rather than the section having been mutated in place."""
        stub = _ConfigStub("INFO")
        factory = _logging_settings_factory(stub, verbose=True)  # type: ignore[arg-type]
        factory()
        assert stub.config.logging.level == "INFO"
        assert factory().section.level == "DEBUG"

    def test_logs_dir_comes_from_the_paths(self) -> None:
        stub = _ConfigStub("INFO")
        factory = _logging_settings_factory(stub, verbose=False)  # type: ignore[arg-type]
        assert factory().logs_dir == stub.paths.logs_dir


class TestArgumentSurface:
    """The flags are the product's public interface; a rename is a breaking
    change for anyone's script or shortcut."""

    @pytest.mark.parametrize(
        "flag",
        [
            "--version",
            "--verbose",
            "--config",
            "--wav",
            "--out",
            "--skip-llm",
            "--desktop",
            "--voice",
            "--ingest",
            "--ask",
            "--tools",
            "--memory",
            "--forget",
            "--force",
            "--prompts",
            "--plan",
        ],
    )
    def test_every_documented_flag_exists(self, flag: str) -> None:
        assert any(flag in action.option_strings for action in build_arg_parser()._actions)

    def test_every_flag_has_help_text(self) -> None:
        for action in build_arg_parser()._actions:
            if not action.option_strings:
                continue
            assert action.help, action.option_strings

    def test_short_verbose_is_available(self) -> None:
        args = build_arg_parser().parse_args(["-v"])
        assert args.verbose is True

    def test_defaults_are_all_inactive(self) -> None:
        """Running ``jarvis`` with no arguments must not silently do one of the
        one-shot jobs."""
        args = build_arg_parser().parse_args([])
        assert args.ingest is None
        assert args.ask is None
        assert args.plan is None
        assert args.tools is False
        assert args.memory is False
        assert args.prompts is False
        assert args.desktop is False
        assert args.wav is None


class TestRunCli:
    def test_success_passes_the_exit_code_through(self) -> None:
        assert _run_cli(lambda _args: 0, argparse.Namespace()) == 0

    def test_a_jarvis_error_becomes_one_readable_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A traceback buries a YAML typo under twenty frames of import
        machinery."""

        def boom(_args: argparse.Namespace) -> int:
            raise JarvisError("配置里的 logging.level 写错了")

        assert _run_cli(boom, argparse.Namespace()) == 1
        assert "配置里的 logging.level 写错了" in capsys.readouterr().err

    def test_an_unexpected_error_is_still_a_nonzero_exit(self) -> None:
        def boom(_args: argparse.Namespace) -> int:
            raise RuntimeError("nope")

        assert _run_cli(boom, argparse.Namespace()) == 1

    def test_keyboard_interrupt_is_reported_as_130(self) -> None:
        """The conventional exit code for SIGINT, so a wrapper script can tell
        "the user stopped it" from "it failed"."""
        assert _run_cli(lambda _args: 0, argparse.Namespace()) == 0

        def interrupted(_args: argparse.Namespace) -> int:
            raise KeyboardInterrupt

        assert _run_cli(interrupted, argparse.Namespace()) == 130


class TestVoiceStackGraphWiring:
    """The one line in the composition root that decides what the microphone can do.

    ``graph_factory`` existed as a parameter and nothing was ever passed through it, so
    the desktop fell back to :class:`~jarvis.orchestration.graph.AgentGraph` -- whose
    tools worker has two hard-coded capabilities while the input box had the whole
    registry. A spoken 「本机有没有装 Java」 therefore got a different assistant than the
    typed one, and no test noticed until an operator did.
    """

    def test_the_voice_stack_answers_from_the_graph_the_desktop_hands_over(self) -> None:
        """The factory is the only thing that decides what the microphone can do, so a
        reverted call site -- which is exactly what shipped in r19 -- has to fail here."""
        chat = ChatService(lambda: FakeLlmClient("装了，Temurin 17"), session_id="t")
        chat.start()
        build = _voice_stack_builder(
            _ConfigStub("INFO"),  # type: ignore[arg-type]
            cast(Any, object()),
            graph_factory=lambda: ChatGraph(chat),
        )

        stack = cast(_VoiceStack, build(lambda event: None))
        factory = stack._orchestration._graph_factory
        assert factory is not None, "语音这条路没接到桌面的 agent 上"

        graph = factory()

        assert isinstance(graph, ChatGraph)
        assert graph.run("本机有没有装 Java") == "装了，Temurin 17"


class TestVoiceStackGreetingWiring:
    """The other two things the desktop has to hand the microphone: the words, and the
    permission to say them.

    ``graph_factory`` above is the precedent for why this needs a test at all: the
    parameter existed, nothing was passed through it, and the feature silently did not.
    The wake greeting is wired the same way -- one :class:`WakeGreeter`, two callables --
    and forgetting either ``greeter=`` produces a wake word that says nothing, with
    nothing in the log to say why.
    """

    def test_the_greeter_reaches_the_pipeline_as_words_and_as_a_gate(self) -> None:
        greeter = WakeGreeter(lambda: "在呢，请讲")
        build = _voice_stack_builder(
            _ConfigStub("INFO"),  # type: ignore[arg-type]
            cast(Any, object()),
            greeter=greeter,
        )

        stack = cast(_VoiceStack, build(lambda event: None))
        provider = stack._orchestration._greeting_provider
        gate = stack._orchestration._greeting_gate

        assert provider is not None, "唤醒问候语没接到语音链路上"
        assert gate is not None, "等人物显示的闸门没接到语音链路上"
        assert provider() == "在呢，请讲"
        assert gate(lambda: False) is True

    def test_no_greeter_is_a_quiet_wake_word_not_a_broken_one(self) -> None:
        """The console path never builds a greeter; the voice stack must still start."""
        build = _voice_stack_builder(
            _ConfigStub("INFO"),  # type: ignore[arg-type]
            cast(Any, object()),
        )

        stack = cast(_VoiceStack, build(lambda event: None))

        assert stack._orchestration._greeting_provider is None
        assert stack._orchestration._greeting_gate is None


def _plain_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    """Calls written as ``name(...)`` -- an import from another module, or a local."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _qualified_calls(tree: ast.AST, qualified: str) -> list[ast.Call]:
    """Calls written as ``module.name(...)``."""
    module, _, name = qualified.rpartition(".")
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == module
    ]


def _keywords(call: ast.Call) -> set[str]:
    return {keyword.arg for keyword in call.keywords if keyword.arg}


class TestCompositionRootWiresBothSides:
    """The call sites, which the class above cannot see.

    ``_voice_stack_builder`` honouring its ``greeter`` argument is only half of it: the
    desktop has to *hand it over*, in both directions -- to the voice stack that asks,
    and to the shell that knows when a figure is on its way. Either one missing is a wake
    word that greets too early or never at all, and this module is the only place that
    holds both halves, so this is where the claim gets checked. The syntax tree rather
    than the text, so a renamed argument or a dropped keyword is a failure instead of a
    substring that happens to still be there.
    """

    def test_the_greeter_is_built_from_the_settings_and_passed_to_both_consumers(
        self,
    ) -> None:
        source = (Path(__file__).resolve().parent.parent / "jarvis" / "__main__.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        built = _plain_calls(tree, "WakeGreeter")
        assert len(built) == 1, "the composition root should own exactly one greeter"
        assert "wake_greeting" in ast.unparse(built[0]), "its words are not the editable setting"

        stacks = _plain_calls(tree, "_voice_stack_builder")
        assert len(stacks) == 1
        assert "greeter" in _keywords(stacks[0]), "语音链路没拿到问候语"

        shells = _qualified_calls(tree, "desktop.run")
        assert len(shells) == 1
        assert "greeter" in _keywords(shells[0]), "界面壳子没拿到问候语的闸门"

    def test_the_wake_words_reach_the_engine_that_listens_not_just_the_panel(self) -> None:
        """A rename that saves but is never read is the same bug in a new place.

        Three call sites have to agree for 「唤醒词」 to be real: the object is built once
        over the settings store and the configured defaults, the panel is given it so the
        box can show what is in force, and the voice stack is given a reader so the wake
        engine re-reads it instead of keeping the list it baked at startup. Drop any one and
        the symptom is "I saved and nothing happened", with every gate still green.
        """
        source = (Path(__file__).resolve().parent.parent / "jarvis" / "__main__.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        built = _plain_calls(tree, "WakeWords")
        assert len(built) == 1, "the composition root should own exactly one wake-word layer"
        assert "preferences" in ast.unparse(built[0].args[0]), "它不该绕过设置存储"
        assert "wakeword.keywords" in ast.unparse(built[0]), "默认值该来自配置，不是又抄一份"

        settings = _plain_calls(tree, "SettingsService")
        assert len(settings) == 1
        assert "wake_words" in _keywords(settings[0]), "面板拿不到唤醒词这一层"

        stacks = _plain_calls(tree, "_voice_stack_builder")
        assert len(stacks) == 1
        assert "keywords_provider" in _keywords(stacks[0]), "语音链路没拿到唤醒词的读法"

        shown = _plain_calls(tree, "VoiceService")
        assert len(shown) == 1
        status = _keywords(shown[0])
        assert "keywords" in status, "状态栏那条'她叫什么'也得是改过之后的"
