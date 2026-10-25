"""Tests for :class:`jarvis.app.preferences.Preferences`.

The whole point of this component is that it is on disk, so these tests use a real
file in ``tmp_path`` rather than a fake -- an in-memory double would prove the
logic and miss the bug, and the bug here is the write.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.preferences import (
    MIRROR_MODE,
    SHELL_TIER,
    VIDEO_CLOUD_MODEL,
    VOICE_AUTO_ARM,
    Preferences,
)


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    store = Preferences(tmp_path / "preferences.json")
    assert store.flag(VOICE_AUTO_ARM) is False
    assert not store.path.exists()


def test_choice_survives_a_new_instance(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    Preferences(path).set_flag(VOICE_AUTO_ARM, True)
    assert Preferences(path).flag(VOICE_AUTO_ARM) is True


def test_file_is_plain_json_a_human_can_read(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    Preferences(path).set_flag(VOICE_AUTO_ARM, True)
    assert json.loads(path.read_text(encoding="utf-8")) == {VOICE_AUTO_ARM: True}


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "preferences.json"
    Preferences(path).set_flag(VOICE_AUTO_ARM, True)
    assert path.is_file()


def test_rewriting_the_same_value_does_not_touch_the_disk(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    store = Preferences(path)
    store.set_flag(VOICE_AUTO_ARM, True)
    before = path.read_bytes()
    store.set_flag(VOICE_AUTO_ARM, True)
    assert path.read_bytes() == before


def test_corrupt_file_starts_clean_instead_of_failing_to_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "preferences.json"
    path.write_text("{ not json", encoding="utf-8")
    store = Preferences(path)
    with caplog.at_level(logging.WARNING):
        assert store.flag(VOICE_AUTO_ARM) is False
    assert "unreadable" in caplog.text
    # A broken file is a reason to ignore the old choice, not to keep refusing
    # to record a new one.
    store.set_flag(VOICE_AUTO_ARM, True)
    assert Preferences(path).flag(VOICE_AUTO_ARM) is True


def test_unexpected_content_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert Preferences(path).flag(VOICE_AUTO_ARM) is False


def test_only_known_keys_are_kept(tmp_path: Path) -> None:
    """Somebody else's entry in this file is not ours to carry around."""
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"someone_elses_key": "hello", VOICE_AUTO_ARM: True}), "utf-8")
    store = Preferences(path)
    assert store.flag(VOICE_AUTO_ARM) is True
    store.set_flag(VOICE_AUTO_ARM, False)
    assert "someone_elses_key" not in json.loads(path.read_text(encoding="utf-8"))


def test_non_boolean_value_is_not_treated_as_true(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({VOICE_AUTO_ARM: "yes"}), encoding="utf-8")
    assert Preferences(path).flag(VOICE_AUTO_ARM) is False


def test_unwritable_file_costs_a_log_not_the_window(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "preferences.json"
    blocked.mkdir()  # a directory where the file wants to be: every write fails
    store = Preferences(blocked)
    with caplog.at_level(logging.ERROR):
        store.set_flag(VOICE_AUTO_ARM, True)  # must not raise
    assert store.flag(VOICE_AUTO_ARM) is True  # the in-process answer still holds
    assert "could not save" in caplog.text


def test_read_is_cached_within_one_instance(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    store = Preferences(path)
    assert store.flag(VOICE_AUTO_ARM) is False
    path.write_text(json.dumps({VOICE_AUTO_ARM: True}), encoding="utf-8")
    # Deliberate: one process must not change its mind because something else
    # edited the file mid-conversation.
    assert store.flag(VOICE_AUTO_ARM) is False


def test_every_exported_key_is_registered_for_round_tripping() -> None:
    """A key that is spelled but not registered writes nothing at all.

    This is not hypothetical: ``llm.tuning`` shipped with a constant, a reader, a
    writer and a dropdown, but no entry in ``PREFERENCE_TYPES`` -- so ``set``
    refused every save with nothing louder than a log line, and the per-model
    thinking level looked like it worked until the next question was answered
    with the old one. Enumerating the module's own constants is what catches it,
    because a hand-written list of keys is the thing that was already wrong.
    """
    import jarvis.app.preferences as module

    declared = {
        value
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str) and "." in value
    }

    assert declared - set(module.PREFERENCE_TYPES) == set()


def test_a_registered_key_really_writes_a_dict(tmp_path: Path) -> None:
    """The concrete half of the check above: the tuning table has to land on disk."""
    from jarvis.app.preferences import LLM_TUNING

    path = tmp_path / "preferences.json"
    store = Preferences(path)
    table = {"alpha\x00alpha-1": {"thinking": "high", "turns": 20}}

    assert store.set(LLM_TUNING, table) is True
    assert Preferences(path).get(LLM_TUNING) == table


class TestPersistenceIsHonestAboutItself:
    """OPT-02 regression: "applied" and "on disk" are two different facts.

    Migrated from the r93 review's isolated probe with the same assertions. The
    original failure was that a read-only disk still answered ``True``, and that a
    retry of the same value after the disk recovered skipped the write because the
    cache already matched -- so a 命令行档位 downgrade came back at the next start.
    """

    def test_a_failed_disk_write_is_not_reported_as_saved(self, tmp_path: Path) -> None:
        file = tmp_path / "preferences.json"
        prefs = Preferences(file)
        assert prefs.set(SHELL_TIER, 2)

        def deny(_values: dict[str, Any]) -> None:
            raise PermissionError("simulated read-only disk")

        prefs._write = deny  # type: ignore[method-assign,assignment]
        assert prefs.set(SHELL_TIER, 0) is False
        assert Preferences(file).number(SHELL_TIER) == 2, "文件里还是旧档位，这正是要报出来的原因"
        assert prefs.number(SHELL_TIER) == 0, "但这一轮已经收紧了：降级不能等写盘成功才生效"
        assert prefs.unsaved() == [SHELL_TIER]

    def test_retrying_the_same_value_after_the_disk_recovers_writes_it(
        self, tmp_path: Path
    ) -> None:
        file = tmp_path / "preferences.json"
        prefs = Preferences(file)
        assert prefs.set(SHELL_TIER, 2)

        def deny(_values: dict[str, Any]) -> None:
            raise PermissionError("simulated read-only disk")

        prefs._write = deny  # type: ignore[method-assign,assignment]
        assert prefs.set(SHELL_TIER, 0) is False
        prefs._write = Preferences(file)._write  # type: ignore[method-assign]
        assert prefs.set(SHELL_TIER, 0) is True, "同值 + 脏标记 = 必须真的再写一次"
        assert json.loads(file.read_text(encoding="utf-8"))[SHELL_TIER] == 0
        assert prefs.unsaved() == []

    def test_a_repair_pass_clears_everything_still_pending(self, tmp_path: Path) -> None:
        file = tmp_path / "preferences.json"
        prefs = Preferences(file)

        def deny(_values: dict[str, Any]) -> None:
            raise PermissionError("simulated read-only disk")

        prefs._write = deny  # type: ignore[method-assign,assignment]
        prefs.set(SHELL_TIER, 1)
        prefs.set(MIRROR_MODE, "scan")
        assert set(prefs.unsaved()) == {SHELL_TIER, MIRROR_MODE}
        assert set(prefs.retry_unsaved()) == {SHELL_TIER, MIRROR_MODE}
        prefs._write = Preferences(file)._write  # type: ignore[method-assign]
        assert prefs.retry_unsaved() == []
        saved = json.loads(file.read_text(encoding="utf-8"))
        assert saved[SHELL_TIER] == 1 and saved[MIRROR_MODE] == "scan"

    def test_unset_removes_the_key_so_defaults_apply_after_a_restart(self, tmp_path: Path) -> None:
        file = tmp_path / "preferences.json"
        prefs = Preferences(file)
        assert prefs.set(VIDEO_CLOUD_MODEL, "kling-v1")
        assert prefs.text(VIDEO_CLOUD_MODEL, default="wanx") == "kling-v1"
        assert prefs.unset(VIDEO_CLOUD_MODEL) is True
        assert prefs.unsaved() == []
        assert VIDEO_CLOUD_MODEL not in json.loads(file.read_text(encoding="utf-8"))
        assert Preferences(file).text(VIDEO_CLOUD_MODEL, default="wanx") == "wanx"

    def test_an_explicit_empty_string_is_not_the_same_as_unset(self, tmp_path: Path) -> None:
        file = tmp_path / "preferences.json"
        prefs = Preferences(file)
        assert prefs.set(VIDEO_CLOUD_MODEL, "")
        assert prefs.text(VIDEO_CLOUD_MODEL, default="wanx") == ""
        assert prefs.unset("not.a.real.key") is False

    def test_unset_still_succeeds_when_the_key_was_never_stored(self, tmp_path: Path) -> None:
        prefs = Preferences(tmp_path / "preferences.json")
        assert prefs.unset(VIDEO_CLOUD_MODEL) is True
