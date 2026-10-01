"""Hotkeys and the logon switch: what they promise, and what they do when refused.

Both are thin wrappers over a Windows API, so the tests aim at the two things that
are actually this module's own responsibility: the bookkeeping when a registration
fails, and reading the registry back instead of trusting the write.
"""

from __future__ import annotations

import ctypes
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from jarvis.ui import autostart
from jarvis.ui.hotkeys import SHOW_CHORD, TALK_CHORD, Binding, Hotkeys, default_bindings


class FakeUser32:
    def __init__(self, refuse: tuple[int, ...] = ()) -> None:
        self.refuse = refuse
        self.registered: list[tuple[int, int, int]] = []
        self.unregistered: list[int] = []

    def RegisterHotKey(self, _hwnd: Any, index: int, mods: int, key: int) -> int:  # noqa: N802
        if index in self.refuse:
            return 0
        self.registered.append((index, mods, key))
        return 1

    def UnregisterHotKey(self, _hwnd: Any, index: int) -> int:  # noqa: N802
        self.unregistered.append(index)
        return 1

    def PeekMessageW(self, message: Any, *_args: Any, **_kwargs: Any) -> int:  # noqa: N802
        return 0


def patched(monkeypatch: pytest.MonkeyPatch, fake: FakeUser32) -> None:
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=fake), raising=False)


class TestBindings:
    def test_the_two_chords_the_docs_promise(self) -> None:
        bindings = default_bindings(on_talk=lambda: None, on_toggle=lambda: None)
        assert [binding.name for binding in bindings] == [TALK_CHORD, SHOW_CHORD]
        assert {binding.virtual_key for binding in bindings} == {0x4B, 0x48}
        assert all(binding.modifiers == 0x3 for binding in bindings)  # MOD_CONTROL | MOD_ALT

    def test_describe_reads_as_a_sentence_for_a_person(self) -> None:
        binding = default_bindings(on_talk=lambda: None, on_toggle=lambda: None)[0]
        assert binding.describe() == "说一句话（Ctrl+Alt+K）"


class TestHotkeys:
    def test_nothing_started_reports_not_running(self) -> None:
        hotkeys = Hotkeys(default_bindings(on_talk=lambda: None, on_toggle=lambda: None))
        assert hotkeys.running is False
        assert hotkeys.describe() == "全局热键未启用"
        hotkeys.stop()  # idempotent

    def test_both_chords_register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeUser32()
        patched(monkeypatch, fake)
        hotkeys = Hotkeys(default_bindings(on_talk=lambda: None, on_toggle=lambda: None))
        assert hotkeys.start() is True
        assert hotkeys.registered == 2
        assert [entry[0] for entry in fake.registered] == [1, 2]
        hotkeys.stop()

    def test_a_taken_chord_is_reported_and_the_other_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeUser32(refuse=(1,))
        patched(monkeypatch, fake)
        hotkeys = Hotkeys(default_bindings(on_talk=lambda: None, on_toggle=lambda: None))
        assert hotkeys.start() is True
        assert hotkeys.registered == 1
        assert hotkeys.failures == [TALK_CHORD]
        assert TALK_CHORD in hotkeys.describe() and "未生效" in hotkeys.describe()
        hotkeys.stop()

    def test_the_survivor_keeps_its_own_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Renumbering the survivors would fire the wrong handler on the next press."""
        fake = FakeUser32(refuse=(1,))
        patched(monkeypatch, fake)
        hotkeys = Hotkeys(default_bindings(on_talk=lambda: None, on_toggle=lambda: None))
        hotkeys.start()
        # The second chord must still be the one bound to index 2.
        assert fake.registered == [(2, 0x3, 0x48)]
        hotkeys.stop()

    def test_every_chord_refused_is_a_failure_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patched(monkeypatch, FakeUser32(refuse=(1, 2)))
        hotkeys = Hotkeys(default_bindings(on_talk=lambda: None, on_toggle=lambda: None))
        assert hotkeys.start() is False
        assert hotkeys.registered == 0

    def test_a_handler_that_throws_leaves_the_loop_alive(self) -> None:
        from jarvis.ui.hotkeys import _invoke

        def explode() -> None:
            raise RuntimeError("窗口不肯出来")

        _invoke(Binding("Ctrl+Alt+K", "说", 3, 0x4B, explode))  # must not raise

    def test_starting_twice_does_not_register_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeUser32()
        patched(monkeypatch, fake)
        hotkeys = Hotkeys(default_bindings(on_talk=lambda: None, on_toggle=lambda: None))
        assert hotkeys.start() is True
        assert hotkeys.start() is True
        assert len(fake.registered) == 2
        hotkeys.stop()


class FakeWinReg:
    """A registry that only knows the one key this module touches."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, values: dict[str, str] | None = None, *, missing: bool = False) -> None:
        self.values = dict(values or {})
        self.missing = missing
        self.deleted: list[str] = []

    def OpenKey(self, _root: Any, name: str, *_args: Any) -> Any:  # noqa: N802
        if self.missing:
            raise FileNotFoundError(name)
        return ("handle", name)

    def CloseKey(self, _handle: Any) -> None:  # noqa: N802
        return None

    def QueryValueEx(self, _key: Any, name: str) -> tuple[Any, int]:  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, _key: Any, name: str, *_args: Any) -> None:  # noqa: N802
        # The real signature is (key, name, reserved, type, value).
        self.values[name] = _args[2]

    def DeleteValue(self, _key: Any, name: str) -> None:  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]
        self.deleted.append(name)

    def __enter__(self) -> FakeWinReg:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def with_registry(monkeypatch: pytest.MonkeyPatch, fake: FakeWinReg) -> FakeWinReg:
    # ``OpenKey`` has to return a context manager; the module uses ``with``.
    fake.OpenKey = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


EXE = Path("E:/BianChengGongJu/JarvisBuild/r13/dist/小夜/小夜.exe")


class TestAutostart:
    def test_the_command_is_quoted_because_spaces_exist_in_paths(self) -> None:
        assert autostart.command_for(EXE) == f'"{EXE.as_posix()}"'

    def test_no_key_at_all_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with_registry(monkeypatch, FakeWinReg(missing=True))
        assert autostart.enabled(EXE) is False

    def test_a_matching_value_means_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = with_registry(monkeypatch, FakeWinReg())
        fake.values[autostart.VALUE_NAME] = autostart.command_for(EXE)
        assert autostart.enabled(EXE) is True

    def test_a_stale_path_from_an_older_build_means_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason the state is read back: r12's entry is not this exe."""
        fake = with_registry(monkeypatch, FakeWinReg())
        fake.values[autostart.VALUE_NAME] = '"E:/old/小夜.exe"'
        assert autostart.enabled(EXE) is False

    def test_enabling_then_disabling_leaves_no_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = with_registry(monkeypatch, FakeWinReg())
        assert autostart.set_enabled(EXE, True) == (True, "")
        assert fake.values[autostart.VALUE_NAME] == autostart.command_for(EXE)
        assert autostart.set_enabled(EXE, False) == (False, "")
        assert autostart.VALUE_NAME not in fake.values

    def test_turning_off_something_already_absent_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_registry(monkeypatch, FakeWinReg())
        assert autostart.set_enabled(EXE, False) == (False, "")

    def test_a_write_that_did_not_take_is_reported_as_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The checkbox must show what the registry says, not what was asked for."""
        fake = with_registry(monkeypatch, FakeWinReg())

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise PermissionError("策略拦住了")

        fake.SetValueEx = refuse  # type: ignore[method-assign]
        now, error = autostart.set_enabled(EXE, True)
        assert now is False and "改不了" in error
