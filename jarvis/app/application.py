"""Application lifecycle management for JARVIS.

:class:`Application` is the composition root of the whole assistant. Every
subsystem delivered in later phases (config, logging, LLM, wake word, ASR,
TTS, agents, memory, UI, ...) is registered as a :class:`LifecycleComponent`
and started/stopped in a deterministic order:

* start:  registration order
* stop:   reverse registration order
* a failure during start rolls back every already-started component

This keeps subsystem wiring in exactly one place and gives the rest of the
codebase a single, testable lifecycle contract.
"""

from __future__ import annotations

import logging
from enum import Enum, unique
from typing import Protocol, runtime_checkable

from jarvis.core.exceptions import JarvisError

logger = logging.getLogger(__name__)


@runtime_checkable
class LifecycleComponent(Protocol):
    """Contract for any subsystem managed by :class:`Application`."""

    @property
    def name(self) -> str:
        """Unique, human-readable component name (used in logs and errors)."""
        ...

    def start(self) -> None:
        """Acquire resources and become operational. May raise on failure."""
        ...

    def stop(self) -> None:
        """Release resources. Must be idempotent and should not raise."""
        ...


@unique
class AppState(Enum):
    """Lifecycle states of the application."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class ComponentStartError(JarvisError):
    """Raised when a component fails during application startup."""

    def __init__(self, component_name: str, cause: BaseException) -> None:
        super().__init__(
            f"component '{component_name}' failed to start: {cause}",
            details={"component": component_name},
        )
        self.component_name = component_name
        self.cause = cause


class Application:
    """Composition root: owns subsystems and drives their lifecycle."""

    def __init__(self) -> None:
        self._components: list[LifecycleComponent] = []
        self._state: AppState = AppState.CREATED

    @property
    def state(self) -> AppState:
        """Current lifecycle state."""
        return self._state

    @property
    def components(self) -> tuple[LifecycleComponent, ...]:
        """Registered components, in registration (startup) order."""
        return tuple(self._components)

    def register(self, component: LifecycleComponent) -> None:
        """Register a component. Only allowed before :meth:`start`.

        Raises:
            RuntimeError: if the application has already been started.
            ValueError: if a component with the same name is already registered.
        """
        if self._state is not AppState.CREATED:
            raise RuntimeError(
                f"components can only be registered in state "
                f"'{AppState.CREATED.value}', current state is '{self._state.value}'"
            )
        if any(existing.name == component.name for existing in self._components):
            raise ValueError(f"duplicate component name: {component.name!r}")
        self._components.append(component)
        logger.debug("component registered: %s", component.name)

    def start(self) -> None:
        """Start all components in registration order.

        If any component fails, every component that was already started is
        stopped in reverse order, and :class:`ComponentStartError` is raised.
        """
        if self._state not in (AppState.CREATED, AppState.STOPPED):
            raise RuntimeError(f"cannot start application in state '{self._state.value}'")

        self._state = AppState.STARTING
        started: list[LifecycleComponent] = []
        for component in self._components:
            try:
                logger.debug("starting component: %s", component.name)
                component.start()
                started.append(component)
                logger.info("component started: %s", component.name)
            except Exception as exc:
                logger.exception("component failed to start: %s", component.name)
                self._stop_components(started)
                self._state = AppState.STOPPED
                raise ComponentStartError(component.name, exc) from exc

        self._state = AppState.RUNNING
        logger.info("application running with %d component(s)", len(self._components))

    def stop(self) -> None:
        """Stop all components in reverse order. Safe to call multiple times."""
        if self._state is not AppState.RUNNING:
            logger.debug("stop() ignored in state '%s'", self._state.value)
            return

        self._state = AppState.STOPPING
        self._stop_components(list(self._components))
        self._state = AppState.STOPPED
        logger.info("application stopped")

    @staticmethod
    def _stop_components(components: list[LifecycleComponent]) -> None:
        """Stop the given components in reverse order, never raising."""
        for component in reversed(components):
            try:
                component.stop()
                logger.info("component stopped: %s", component.name)
            except Exception:
                logger.exception("component failed to stop: %s", component.name)
