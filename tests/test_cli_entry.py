"""Tests for the command-line entry point's helpers (jarvis/__main__.py).

``__main__`` is the composition root and mostly untestable without starting the
whole application, but two of its pieces are pure logic with real failure modes:
the logging settings factory, and the argument surface itself.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from jarvis.__main__ import _logging_settings_factory, _run_cli, build_arg_parser
from jarvis.config import AppPaths
from jarvis.config.loader import load_defaults
from jarvis.config.schema import AppConfig
from jarvis.core.exceptions import JarvisError


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
