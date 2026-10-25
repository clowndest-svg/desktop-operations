from pathlib import Path

from jarvis.app.portrait_preferences import portrait_preferences
from jarvis.app.preferences import Preferences


def test_bounded_choices_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    choices = portrait_preferences(
        {
            "character": "p20",
            "gaze": False,
            "bindings": {"p20": "zh-CN-XiaoyiNeural", "unknown": "bad"},
        }
    )
    assert Preferences(path).set("ui.portrait", choices)
    assert portrait_preferences(Preferences(path).get("ui.portrait")) == {
        "character": "p20",
        "gaze": False,
        "emotions": True,
        "bindings": {"p20": "zh-CN-XiaoyiNeural"},
    }


def test_malformed_values_are_safe() -> None:
    values: tuple[object, ...] = (
        None,
        [],
        {"character": []},
        {"bindings": []},
        {"character": "p99"},
    )
    for value in values:
        assert portrait_preferences(value)["character"] == "p01"
    assert portrait_preferences({"bindings": {"p01": "x" * 200}})["bindings"] == {}
