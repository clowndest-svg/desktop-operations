"""Browser automation types: the page snapshot and the engine contract.

``PageContent`` is what the layers above actually consume — a plain, JSON-ready
snapshot of one page. ``BrowserEngine`` is the narrow seam the service drives,
so a Playwright session can be swapped for a fake in tests and the service never
has to know which driver is underneath.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PageContent:
    """A read-only snapshot of one page.

    Frozen and tuple-valued on purpose: this crosses into ``tools``/``app`` and
    is eventually handed to the model, so no caller may mutate what another
    caller has already rendered.
    """

    url: str
    title: str
    text: str
    """Body text, already truncated to the configured ``max_text_chars``."""

    links: tuple[tuple[str, str], ...]
    """``(anchor text, href)`` pairs, capped by the engine."""

    truncated: bool
    """True when ``text`` was cut, so the model can be told it saw only part."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready form for the UI bridge and the model prompt."""
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "links": [list(pair) for pair in self.links],
            "truncated": self.truncated,
        }


@runtime_checkable
class BrowserEngine(Protocol):
    """The driver seam: navigate, interact, extract.

    A Protocol rather than a base class because the real implementation depends
    on an optional, heavy SDK. The service is typed against this, so the test
    suite drives a fake engine and never launches a browser.
    """

    @property
    def name(self) -> str:
        """Short engine identifier, for logs and ``stats()``."""
        ...

    def start(self) -> None:
        """Launch the driver. Must be idempotent."""
        ...

    def stop(self) -> None:
        """Release the driver. Safe to call when it was never started."""
        ...

    def goto(self, url: str) -> PageContent:
        """Navigate and return the resulting page."""
        ...

    def click(self, selector: str) -> None:
        """Click the first element matching ``selector``."""
        ...

    def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the field matching ``selector``."""
        ...

    def screenshot(self, path: str) -> str:
        """Save a screenshot to ``path``; return the path written."""
        ...

    def content(self) -> PageContent:
        """Re-read the *current* page without navigating.

        Needed because ``read()`` and ``click()`` must report the live page.
        Without this the only route back to a snapshot would be to re-``goto``,
        which would discard the effect of the interaction that just happened.
        """
        ...
