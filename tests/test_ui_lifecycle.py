"""The hide-instead-of-quit policy, tested without a window.

``ShellLifecycle`` exists so the whole question -- "what does the X do, and how do I
actually leave" -- can be answered by a test instead of by a person hunting for a
process in Task Manager. The fakes below are the contract that has to hold: pywebview
calls ``closing`` handlers on the UI thread and reads ``False`` as "cancel".
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from jarvis.core.events import PIPELINE_STATE_KIND, VOICE_STATUS_KIND, PipelineEvent
from jarvis.ui.lifecycle import BACKGROUND_SCRIPT, ShellLifecycle
from jarvis.ui.tray import (
    STATUS_HEARING,
    STATUS_OFF,
    STATUS_WAITING,
    STATUS_WORKING,
)


class FakeTray:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.statuses: list[str] = []
        self.messages: list[str] = []
        self.stopped = 0

    def set_status(self, status: str) -> None:
        self.statuses.append(status)

    def notify(self, message: str, title: str = "小夜") -> bool:
        self.messages.append(message)
        return True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.scripts: list[str] = []

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)


class FakePet:
    """The pet window: a second "screen" that can hold the microphone's answer."""

    def __init__(self) -> None:
        self._shown = False
        self.toggles = 0
        self.summons = 0
        self.closed = 0

    @property
    def shown(self) -> bool:
        return self._shown

    @property
    def audio_window(self) -> str | None:
        return "pet-window" if self._shown else None

    def toggle(self) -> bool:
        self.toggles += 1
        self._shown = not self._shown
        return self._shown

    def summon(self) -> bool:
        self.summons += 1
        self._shown = True
        return True

    def close(self) -> None:
        self.closed += 1
        self._shown = False


def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """``on_closing`` defers its hide to a thread, so assertions have to wait for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def shell(
    *, running: bool = True, hide_to_tray: bool = True
) -> tuple[ShellLifecycle, FakeWindow, FakeTray]:
    window = FakeWindow()
    tray = FakeTray(running=running)
    lifecycle = ShellLifecycle(window, tray=tray, hide_to_tray=hide_to_tray)
    return lifecycle, window, tray


class TestTheX:
    def test_closing_returns_false_because_false_means_cancel(self) -> None:
        """pywebview cancels a close when a handler returns exactly ``False``.

        Written as its own test because it reads backwards: a future editor who
        "tidies" this into ``return True`` would turn the X back into a quit, and
        every other assertion here would still pass.
        """
        lifecycle, window, _ = shell()
        assert lifecycle.on_closing() is False
        assert wait_for(lambda: "hide" in window.calls)

    def test_the_window_is_hidden_not_destroyed(self) -> None:
        lifecycle, window, _ = shell()
        lifecycle.on_closing()
        wait_for(lambda: "hide" in window.calls)
        assert "destroy" not in window.calls

    def test_no_tray_means_the_close_button_really_quits(self) -> None:
        """An invisible app with no visible way back is worse than no feature."""
        lifecycle, window, _ = shell(running=False)
        assert lifecycle.on_closing() is True
        assert window.calls == []

    def test_hiding_while_exiting_does_not_reopen_the_argument(self) -> None:
        """The tray is left *running* here so only the exiting flag can answer."""
        tray = FakeTray()
        tray.stop = lambda: None  # type: ignore[method-assign]
        lifecycle = ShellLifecycle(FakeWindow(), tray=tray)
        lifecycle.quit()
        assert tray.running and lifecycle.hides_to_tray
        assert lifecycle.on_closing() is True

    def test_a_failed_tray_start_downgrades_the_policy(self) -> None:
        lifecycle, window, tray = shell()
        tray.running = False
        assert lifecycle.hides_to_tray is False
        assert lifecycle.on_closing() is True
        assert window.calls == []


class TestVisibility:
    def test_hide_tells_the_page_it_is_in_the_background(self) -> None:
        """``document.hidden`` stays false in a hidden WebView2 window; say it directly."""
        lifecycle, window, _ = shell()
        lifecycle.hide()
        assert BACKGROUND_SCRIPT.format(value="false") in window.scripts
        assert lifecycle.visible is False

    def test_show_puts_the_page_back_on_the_clock(self) -> None:
        lifecycle, window, _ = shell()
        lifecycle.hide()
        lifecycle.show()
        assert BACKGROUND_SCRIPT.format(value="true") in window.scripts[-1]
        assert lifecycle.visible is True

    def test_a_minimised_window_is_un_minimised_when_the_tray_is_clicked(self) -> None:
        lifecycle, window, _ = shell()
        lifecycle.on_minimized()
        lifecycle.show()
        assert window.calls == ["show", "restore"]

    def test_show_does_not_undo_a_maximised_window(self) -> None:
        """``restore()`` also un-maximises, so it is only asked for after a minimise."""
        lifecycle, window, _ = shell()
        lifecycle.show()
        assert window.calls == ["show"]

    def test_toggle_flips_in_both_directions(self) -> None:
        lifecycle, _, _ = shell()
        assert lifecycle.toggle() is False
        assert lifecycle.toggle() is True

    def test_the_first_hide_explains_where_the_app_went(self) -> None:
        lifecycle, _, tray = shell()
        lifecycle.hide()
        assert len(tray.messages) == 1
        assert "托盘" in tray.messages[0]

    def test_repeated_hides_do_not_shout(self) -> None:
        lifecycle, _, tray = shell()
        lifecycle.hide()
        lifecycle.show()
        lifecycle.hide()
        assert len(tray.messages) == 1

    def test_hide_without_a_window_fails_quietly(self) -> None:
        lifecycle = ShellLifecycle(tray=FakeTray())
        assert lifecycle.hide() is False
        assert lifecycle.show() is False

    def test_a_window_that_refuses_to_hide_is_reported_not_raised(self) -> None:
        class Stubborn(FakeWindow):
            def hide(self) -> None:
                raise RuntimeError("GUI 线程没应答")

        lifecycle = ShellLifecycle(Stubborn(), tray=FakeTray())
        assert lifecycle.hide() is False
        assert lifecycle.visible is True


class TestVoiceReachesTheIcon:
    def test_running_phase_says_the_microphone_is_open(self) -> None:
        lifecycle, _, tray = shell()
        lifecycle.on_event(PipelineEvent(kind=VOICE_STATUS_KIND, text="running"))
        assert tray.statuses[-1] == STATUS_WAITING
        assert lifecycle.tray_status == STATUS_WAITING

    def test_every_other_phase_says_it_is_not(self) -> None:
        lifecycle, _, tray = shell()
        for phase in ("off", "loading", "failed"):
            lifecycle.on_event(PipelineEvent(kind=VOICE_STATUS_KIND, text=phase))
        assert tray.statuses == [STATUS_OFF, STATUS_OFF, STATUS_OFF]

    @pytest.mark.parametrize(
        ("event", "status"),
        [
            (PipelineEvent(kind=PIPELINE_STATE_KIND, text="listening"), STATUS_HEARING),
            (PipelineEvent(kind=PIPELINE_STATE_KIND, text="processing"), STATUS_WORKING),
            (PipelineEvent(kind=PIPELINE_STATE_KIND, text="idle"), STATUS_WAITING),
            (PipelineEvent(kind="speech_start"), STATUS_HEARING),
            (PipelineEvent(kind="wake"), STATUS_HEARING),
            (PipelineEvent(kind="barge_in"), STATUS_HEARING),
            (PipelineEvent(kind="speech_end"), STATUS_WORKING),
        ],
    )
    def test_pipeline_milestones_map_onto_the_icon(self, event: PipelineEvent, status: str) -> None:
        lifecycle, _, tray = shell()
        lifecycle.set_voice_running(True)
        tray.statuses.clear()
        lifecycle.on_event(event)
        assert tray.statuses == [status]

    def test_a_released_microphone_stays_released(self) -> None:
        """An event about the *pipeline* must not resurrect the microphone claim."""
        lifecycle, _, tray = shell()
        lifecycle.on_event(PipelineEvent(kind=VOICE_STATUS_KIND, text="off"))
        tray.statuses.clear()
        lifecycle.on_event(PipelineEvent(kind="speech_start"))
        lifecycle.on_event(PipelineEvent(kind=PIPELINE_STATE_KIND, text="listening"))
        assert tray.statuses == [STATUS_OFF, STATUS_OFF]

    def test_unknown_kinds_and_junk_change_nothing(self) -> None:
        lifecycle, _, tray = shell()
        lifecycle.set_voice_running(True)
        tray.statuses.clear()
        for junk in (object(), None, 42):
            lifecycle.on_event(junk)
        assert tray.statuses == []

    def test_status_survives_without_a_tray(self) -> None:
        lifecycle = ShellLifecycle(FakeWindow(), tray=None)
        lifecycle.on_event(PipelineEvent(kind=VOICE_STATUS_KIND, text="running"))
        assert lifecycle.tray_status == STATUS_WAITING


class TestQuitting:
    def test_quit_stops_the_tray_before_it_touches_the_windows(self) -> None:
        """A person must not be able to click 退出 twice while the first is still running."""
        order: list[str] = []
        window = FakeWindow()
        window.destroy = lambda: order.append("destroy")  # type: ignore[method-assign]
        tray = FakeTray()
        tray.stop = lambda: order.append("tray")  # type: ignore[method-assign]
        lifecycle = ShellLifecycle(window, tray=tray)
        lifecycle.quit()
        assert order == ["tray", "destroy"]
        assert lifecycle.exiting is True

    def test_after_quit_the_next_closing_is_let_through(self) -> None:
        lifecycle, _, _ = shell()
        lifecycle.quit()
        assert lifecycle.on_closing() is True

    def test_quit_also_closes_the_pet(self) -> None:
        lifecycle, _, _ = shell()
        pet = FakePet()
        lifecycle.attach_pet(pet)
        lifecycle.quit()
        assert pet.closed == 1

    def test_quit_is_idempotent(self) -> None:
        lifecycle, window, tray = shell()
        lifecycle.quit()
        lifecycle.quit()
        assert window.calls.count("destroy") == 1
        assert tray.stopped == 1

    def test_a_window_that_will_not_die_does_not_raise(self) -> None:
        class Impossible(FakeWindow):
            def destroy(self) -> None:
                raise RuntimeError("已经没了")

        lifecycle = ShellLifecycle(Impossible(), tray=FakeTray())
        lifecycle.quit()
        assert lifecycle.exiting is True

    def test_shutdown_stops_the_icon_even_if_the_loop_ended_by_itself(self) -> None:
        lifecycle, _, tray = shell()
        lifecycle.shutdown()
        assert tray.stopped == 1


class TestWhoSpeaks:
    """Exactly one window can play the answer, or the operator hears it twice."""

    def shell_with_audio(self) -> tuple[ShellLifecycle, FakeWindow, FakePet, list[object]]:
        window = FakeWindow()
        pet = FakePet()
        owners: list[object] = []
        lifecycle = ShellLifecycle(window, tray=FakeTray(), pet=pet, on_audio_window=owners.append)
        return lifecycle, window, pet, owners

    def test_the_dashboard_speaks_while_it_is_on_screen(self) -> None:
        """A pet on the desktop does not take the voice away from a window in front."""
        lifecycle, window, _pet, owners = self.shell_with_audio()
        lifecycle.toggle_pet()
        assert owners == [window]

    def test_the_pet_speaks_when_the_window_is_in_the_tray(self) -> None:
        lifecycle, window, _pet, owners = self.shell_with_audio()
        lifecycle.toggle_pet()
        lifecycle.hide()
        assert owners == [window, "pet-window"]

    def test_the_dashboard_keeps_the_voice_when_both_are_away(self) -> None:
        """Nothing on screen still has to be able to answer; the HUD is the default."""
        lifecycle, window, pet, owners = self.shell_with_audio()
        lifecycle.hide()
        assert owners == [window]
        assert pet.audio_window is None

    def test_coming_back_hands_the_audio_home(self) -> None:
        lifecycle, window, _pet, owners = self.shell_with_audio()
        lifecycle.toggle_pet()
        lifecycle.hide()
        lifecycle.show()
        assert owners[-1] is window

    def test_a_lifecycle_with_no_hook_still_works(self) -> None:
        """The shell may not care who speaks (tests, headless); visibility must not break."""
        lifecycle, _, _ = shell()
        assert lifecycle.hide() is True


class TestWakeSummonsTheFigure:
    def pet_shell(self) -> tuple[ShellLifecycle, FakePet]:
        lifecycle, _, _ = shell()
        pet = FakePet()
        lifecycle.attach_pet(pet)
        # Through the lifecycle, because that is the only path that remembers to
        # re-decide who holds the microphone.
        lifecycle.toggle_pet()
        return lifecycle, pet

    def test_the_wake_word_while_hidden_brings_the_pet_out(self) -> None:
        lifecycle, pet = self.pet_shell()
        lifecycle.hide()
        pet.summons = 0
        lifecycle.on_event(PipelineEvent(kind="wake"))
        assert pet.summons == 1

    def test_the_wake_word_in_front_of_an_open_window_does_not(self) -> None:
        """The dashboard is already the thing the operator is looking at."""
        lifecycle, pet = self.pet_shell()
        lifecycle.on_event(PipelineEvent(kind="wake"))
        assert pet.summons == 0

    def test_a_wake_word_does_not_switch_the_pet_on_by_itself(self) -> None:
        """A figure appearing unasked is the assistant talking unprompted, with geometry."""
        lifecycle, _, _ = shell()
        pet = FakePet()
        lifecycle.attach_pet(pet)
        lifecycle.hide()
        lifecycle.on_event(PipelineEvent(kind="wake"))
        assert pet.summons == 0

    def test_no_pet_window_means_the_wake_word_changes_only_the_icon(self) -> None:
        lifecycle, _, tray = shell()
        lifecycle.set_voice_running(True)
        tray.statuses.clear()
        lifecycle.hide()
        lifecycle.on_event(PipelineEvent(kind="wake"))
        assert tray.statuses[-1] == STATUS_HEARING


class TestPetHooks:
    def test_toggle_pet_delegates_and_reports(self) -> None:
        lifecycle, _, _ = shell()
        pet = FakePet()
        lifecycle.attach_pet(pet)
        assert lifecycle.toggle_pet() is True
        assert lifecycle.pet_shown() is True
        assert lifecycle.state().pet is True

    def test_no_pet_window_means_no_pet(self) -> None:
        lifecycle, _, _ = shell()
        assert lifecycle.toggle_pet() is False
        assert lifecycle.pet_shown() is False

    def test_state_is_a_plain_mapping_for_the_page(self) -> None:
        lifecycle, _, _ = shell()
        assert lifecycle.to_mapping() == {
            "visible": True,
            "tray": True,
            "status": STATUS_OFF,
            "pet": False,
        }
