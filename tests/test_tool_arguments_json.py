"""Parsing a small model's tool arguments, where Windows paths go to die.

The r89 acceptance caught this on a real Qwen3-4B: it had a working PowerShell tool and
could not hand it a path, because a 4B writes ``C:\\Windows`` with one backslash, which
JSON calls an illegal escape, and the whole call came back as 「参数不是合法 JSON」.

The interesting property is what must NOT change: ``\\n`` and ``\\u4f60`` are legal
escapes that a model may genuinely mean, so a repair that touches them would corrupt
input that was fine. Only escapes JSON itself refuses get doubled.
"""

from __future__ import annotations

from jarvis.app.chat_service import _load_tool_arguments

BS = "\\"


def _wire(body: str) -> str:
    """A JSON document as it arrives on the wire, backslashes and all."""
    return "{" + body + "}"


class TestTheRepair:
    def test_a_windows_path_with_single_backslashes_survives(self) -> None:
        raw = _wire(f'"script": "Get-ChildItem C:{BS}Windows{BS}System32"')
        arguments, problem = _load_tool_arguments(raw)
        assert problem == ""
        assert arguments == {"script": f"Get-ChildItem C:{BS}Windows{BS}System32"}

    def test_a_correctly_escaped_path_is_untouched(self) -> None:
        raw = _wire(f'"script": "Get-ChildItem C:{BS}{BS}Windows"')
        arguments, problem = _load_tool_arguments(raw)
        assert problem == "" and arguments == {"script": f"Get-ChildItem C:{BS}Windows"}

    def test_a_real_newline_escape_still_means_a_newline(self) -> None:
        """Not a path separator: doubling this would turn 两段 into a literal backslash-n."""
        arguments, problem = _load_tool_arguments(_wire(f'"text": "line1{BS}nline2"'))
        assert problem == "" and arguments == {"text": "line1\nline2"}

    def test_a_unicode_escape_still_decodes(self) -> None:
        arguments, problem = _load_tool_arguments(_wire(f'"s": "{BS}u4f60"'))
        assert problem == "" and arguments == {"s": "你"}

    def test_a_tab_escape_still_means_a_tab(self) -> None:
        arguments, problem = _load_tool_arguments(_wire(f'"s": "a{BS}tb"'))
        assert problem == "" and arguments == {"s": "a\tb"}


class TestWhatStillFails:
    def test_an_empty_payload_is_no_arguments_not_an_error(self) -> None:
        assert _load_tool_arguments("   ") == ({}, "")

    def test_a_missing_brace_is_not_patched(self) -> None:
        """Guessing structure would run a tool with arguments nobody wrote."""
        arguments, problem = _load_tool_arguments('{"a": 1')
        assert arguments is None and "不是合法 JSON" in problem

    def test_a_bad_escape_inside_broken_structure_still_fails(self) -> None:
        arguments, problem = _load_tool_arguments('{"p": "' + BS + "x" + BS + 'y" oops')
        assert arguments is None and "不是合法 JSON" in problem

    def test_an_array_is_refused_rather_than_coerced(self) -> None:
        arguments, problem = _load_tool_arguments("[1, 2, 3]")
        assert arguments is None and "JSON 对象" in problem

    def test_a_number_is_refused(self) -> None:
        arguments, problem = _load_tool_arguments("42")
        assert arguments is None and "JSON 对象" in problem
