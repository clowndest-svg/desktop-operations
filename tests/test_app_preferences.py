"""Tests for :class:`jarvis.app.preferences.Preferences`.

The whole point of this component is that it is on disk, so these tests use a real
file in ``tmp_path`` rather than a fake -- an in-memory double would prove the
logic and miss the bug, and the bug here is the write.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jarvis.app.preferences import VOICE_AUTO_ARM, Preferences


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
