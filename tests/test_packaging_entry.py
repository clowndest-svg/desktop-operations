"""Tests for the packaged entry point's two shipped defaults.

``packaging/entry.py`` is not an importable module (``scripts`` and ``packaging``
are not packages), so it is loaded by path the way ``build_desktop`` loads the icon
script. What is under test is the pair of decisions that only exist in the shipped
form: the flags a double-click runs with, and where its data lands when nobody has
configured anything. Both were guessed at before, and one of them put 900 MB of
speech models on the system drive.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_PATH = REPO_ROOT / "packaging" / "entry.py"


@pytest.fixture(scope="module")
def entry() -> Any:
    spec = importlib.util.spec_from_file_location("_jarvis_packaging_entry", ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestShippedFlags:
    def test_a_double_click_gets_desktop_and_voice_permission(self, entry: Any) -> None:
        assert entry.build_argv([]) == ["--desktop", "--voice"]

    def test_the_silhouette_weights_are_carried_into_the_bundle(self) -> None:
        """A frozen app has no package-data: the spec lists every file by hand.

        ``jarvis/vision/silhouette.py`` reads its 4.57 MB of weights through
        ``Path(__file__).parent / "data"``, which resolves under ``_MEIPASS`` in the
        onedir build. Drop the spec line and the app still starts, still opens the
        pet panel, and answers every photo with "抠图模型不在" -- so the check is on
        the two halves together: the file exists in the tree, and the spec ships it.
        """
        from jarvis.vision.silhouette import WEIGHTS_FILE

        root = Path(__file__).resolve().parents[1]
        spec = (root / "packaging" / "jarvis.spec").read_text(encoding="utf-8")

        assert WEIGHTS_FILE.is_file(), WEIGHTS_FILE
        assert WEIGHTS_FILE.stat().st_size > 4_000_000
        assert '"jarvis/vision/data"' in spec
        assert 'os.path.join(ROOT, "jarvis", "vision", "data")' in spec

    def test_caller_arguments_are_still_honoured(self, entry: Any) -> None:
        """``小夜.exe -v`` must keep working: DevTools is the only way to see a page error."""
        assert entry.build_argv(["-v"]) == ["--desktop", "--voice", "-v"]


class TestOneShotModesReachTheFrozenBuild:
    """``--desktop`` is checked *before* the one-shot modes, so injecting it always
    made ``小夜.exe --tools`` open the window instead of printing. The shipped
    artefact was therefore uninspectable from a terminal -- which is how a whole
    verification round ended up launching a GUI on somebody's desktop by accident."""

    def test_a_one_shot_mode_suppresses_the_window_flag(self, entry: Any) -> None:
        assert entry.build_argv(["--tools"]) == ["--voice", "--tools"]

    def test_an_argument_bound_mode_suppresses_it_too(self, entry: Any) -> None:
        assert entry.build_argv(["--ask=你好"]) == ["--voice", "--ask=你好"]

    def test_voice_permission_is_still_granted_alongside_a_one_shot(self, entry: Any) -> None:
        """``--voice`` only *permits* the microphone; the window still needs a press."""
        assert "--voice" in entry.build_argv(["--ask", "你好"])

    def test_every_mode_main_checks_before_the_window_is_listed(self, entry: Any) -> None:
        """The guard against this drifting again.

        Reading ``__main__`` rather than a second list here: a mode added to the CLI
        that nobody remembered to add to ``_ONE_SHOT_MODES`` would be unreachable from
        the .exe, and the only symptom is a flag that works from source and silently
        opens a window from the package.
        """
        import re

        source = (REPO_ROOT / "jarvis" / "__main__.py").read_text(encoding="utf-8")
        window = source.index("if args.desktop:")
        until = source.index("app = Application()", window)
        modes = set(re.findall(r"if args\.([a-z_]+)", source[window:until]))
        modes.discard("desktop")  # that one is the flag being suppressed, not a mode
        assert modes, "读不到 __main__ 里那段一次性模式的分派，这条测试本身失效了"
        flags = {f"--{name.replace('_', '-')}" for name in modes}
        assert (
            flags - entry._ONE_SHOT_MODES == set()
        ), f"这些模式在窗口之前被检查，但打包版永远进不去: {sorted(flags - entry._ONE_SHOT_MODES)}"
