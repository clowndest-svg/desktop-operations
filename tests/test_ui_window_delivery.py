"""What gets a state snapshot, and what happens when one of them has gone away.

The HUD and the desktop pet both show voice state, so both have to be in the push
list. The pet's window is created on demand, which is why registering it belongs to
the pet's own show/hide rather than to whichever code path happened to open it: the
tray item and the button on the bar used to produce a window nobody pushed to, and
the symptom -- a pose that never moved -- was easy to mistake for "she is idle".
"""

from __future__ import annotations

from typing import Any

from jarvis.ui.desktop import _deliver, _pet_window_tracker
from jarvis.ui.state_bridge import UiState


class FakeWindow:
    """A window that records what was pushed into it, and can be told to fail."""

    def __init__(self, name: str = "window", *, broken: bool = False) -> None:
        self.name = name
        self.broken = broken
        self.pushes: list[str] = []

    def evaluate_js(self, script: str) -> None:
        if self.broken:
            raise RuntimeError(f"{self.name} 已经不在了")
        self.pushes.append(script)


class TestPetWindowTracking:
    """The observer that keeps the push list in step with the pet window."""

    def test_showing_the_pet_adds_it_to_the_push_list(self) -> None:
        hud = FakeWindow("hud")
        box: list[Any] = [hud]
        observe = _pet_window_tracker(box)
        pet = FakeWindow("pet")
        observe(pet)
        assert box == [hud, pet]

    def test_the_same_window_is_not_added_twice(self) -> None:
        """``show`` on an already-shown pet must not stack duplicates in the list."""
        box: list[Any] = []
        observe = _pet_window_tracker(box)
        pet = FakeWindow("pet")
        observe(pet)
        observe(pet)
        assert box == [pet]

    def test_hiding_the_pet_drops_only_the_pet(self) -> None:
        """The HUD has to survive: it is the window the operator is looking at."""
        hud = FakeWindow("hud")
        box: list[Any] = [hud]
        observe = _pet_window_tracker(box)
        observe(FakeWindow("pet"))
        observe(None)
        assert box == [hud]

    def test_showing_again_puts_it_back(self) -> None:
        box: list[Any] = []
        observe = _pet_window_tracker(box)
        pet = FakeWindow("pet")
        observe(pet)
        observe(None)
        observe(pet)
        assert box == [pet]

    def test_the_push_list_is_mutated_not_replaced(self) -> None:
        """The state sink captured this list object; rebinding the name orphans it.

        Written as an assertion on an alias, because that is the only way to tell the
        two implementations apart from outside: ``box = [...]`` inside the closure
        would leave this alias holding the stale list and the sink pushing into a
        window list nothing updates any more.
        """
        hud = FakeWindow("hud")
        box: list[Any] = [hud]
        sink_side = box
        observe = _pet_window_tracker(box)
        observe(FakeWindow("pet"))
        observe(None)
        assert sink_side == [hud]


class TestDeliveryIsolation:
    """One window's failure must not become every window's failure."""

    def test_every_window_gets_the_snapshot(self) -> None:
        first = FakeWindow("hud")
        second = FakeWindow("pet")
        _deliver([first, second], UiState.idle())
        assert len(first.pushes) == 1
        assert len(second.pushes) == 1

    def test_one_dead_window_does_not_stop_the_others(self) -> None:
        """The loop used to abort on the first raise, so a dead pet froze the HUD.

        The other half of the damage was invisible here: the pump backs off when a
        push fails, so the surviving window's updates arrived late as well as rarely.
        """
        dead = FakeWindow("pet", broken=True)
        alive = FakeWindow("hud")
        _deliver([dead, alive], UiState.idle())
        assert len(alive.pushes) == 1

    def test_the_dead_window_is_dropped_from_the_box(self) -> None:
        """Retrying it every frame is how one dead window becomes a permanent tax."""
        dead = FakeWindow("pet", broken=True)
        alive = FakeWindow("hud")
        box: list[Any] = [dead, alive]
        _deliver(box, UiState.idle())
        assert box == [alive]

    def test_an_empty_box_is_not_an_error(self) -> None:
        """Before any window exists, snapshots are dropped -- quietly, on purpose."""
        _deliver([], UiState.idle())
