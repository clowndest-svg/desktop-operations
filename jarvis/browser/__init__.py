"""Browser automation (Playwright).

Responsibility:
    * Managed browser sessions, page navigation, DOM interaction and scraping
      helpers, exposed as capabilities to ``tools``/``agent``.

``playwright`` is optional (the ``browser`` extra) and imported lazily, so a
plain install imports this package for free. Every navigation passes
:class:`~jarvis.browser.policy.DomainPolicy` before the engine starts, so a
blocked domain never launches a browser.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.browser.engine import PlaywrightEngine
from jarvis.browser.policy import DomainPolicy
from jarvis.browser.service import BrowserService
from jarvis.browser.types import BrowserEngine, PageContent

__all__ = [
    "BrowserEngine",
    "BrowserService",
    "DomainPolicy",
    "PageContent",
    "PlaywrightEngine",
]
