"""``BrowserService`` — the lifecycle component the upper layers drive.

It owns the one engine and the one domain policy, so ``tools``/``agent`` cannot
each launch their own browser and quietly disagree about what is allowed. The
policy is consulted *before* the engine is started: a blocked URL must cost
nothing and must not open a window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from jarvis.browser.engine import PlaywrightEngine
from jarvis.browser.policy import DomainPolicy
from jarvis.browser.types import BrowserEngine, PageContent
from jarvis.core.exceptions import BrowserError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config.schema import BrowserSection

logger = logging.getLogger("jarvis.browser.service")

_ENABLE_HINT = (
    "请在配置中设置 browser.enabled=true 并安装浏览器内核"
    "（pip install jarvis-assistant[browser] 后执行 playwright install chromium）；"
    "设置环境变量 PLAYWRIGHT_BROWSERS_PATH 可把内核安装到非系统盘，避免占用 C 盘"
)


class BrowserService:
    """Navigate, interact with and read web pages through one managed engine."""

    name = "browser"

    def __init__(
        self,
        settings_provider: Callable[[], BrowserSection],
        *,
        engine: BrowserEngine | None = None,
    ) -> None:
        """Create the service.

        Args:
            settings_provider: Returns the validated ``browser`` config section.
                A provider rather than the section itself because configuration
                does not exist yet at registration time.
            engine: Optional override; tests inject a fake and skip Playwright
                entirely.
        """
        self._settings_provider = settings_provider
        self._override = engine
        self._engine: BrowserEngine | None = None
        self._policy: DomainPolicy | None = None
        self._started = False
        self._last: PageContent | None = None
        self._opened = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Build the policy. Deliberately does not launch the browser.

        Launching is a heavy, network-adjacent step; deferring it to the first
        :meth:`open` keeps startup fast and means a blocked or disabled session
        never pays for a browser it will not use.
        """
        if self._started:
            return
        section = self._settings_provider()
        self._policy = DomainPolicy(allow=section.allow_domains, block=section.block_domains)
        self._started = True
        if section.enabled:
            logger.info(
                "browser service ready (headless=%s, timeout=%.1fs, max_text_chars=%d)",
                section.headless,
                section.timeout_seconds,
                section.max_text_chars,
            )
        else:
            logger.info("browser service ready but disabled (browser.enabled=false)")

    def stop(self) -> None:
        """Shut the engine down and forget the policy (idempotent)."""
        engine = self._engine
        self._engine = None
        self._policy = None
        self._started = False
        self._last = None
        if engine is not None:
            try:
                engine.stop()
            except Exception:  # a teardown failure must not propagate
                logger.debug("error stopping the browser engine", exc_info=True)

    @property
    def running(self) -> bool:
        """Whether :meth:`start` has run and :meth:`stop` has not."""
        return self._started

    # -- operations --------------------------------------------------------

    def open(self, url: str) -> PageContent:
        """Navigate to ``url`` and return the resulting page.

        Raises:
            BrowserError: when the feature is disabled, the service was not
                started, the domain is refused, or navigation fails.
        """
        section = self._require_enabled()
        self._require_policy().check(url)
        engine = self._ensure_engine(section)
        content = engine.goto(url)
        self._last = content
        self._opened += 1
        return content

    def read(self) -> PageContent:
        """Re-read the current page without navigating."""
        self._require_enabled()
        content = self._require_engine().content()
        self._last = content
        return content

    def click(self, selector: str) -> PageContent:
        """Click a matching element, then return the page it produced."""
        self._require_enabled()
        engine = self._require_engine()
        engine.click(selector)
        content = engine.content()
        self._last = content
        return content

    def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the field matching ``selector``."""
        self._require_enabled()
        self._require_engine().fill(selector, value)

    def screenshot(self, path: str) -> str:
        """Save a screenshot of the current page; return the path written."""
        self._require_enabled()
        return self._require_engine().screenshot(path)

    def stats(self) -> dict[str, object]:
        """Read-only snapshot, safe before ``start`` or after ``stop``."""
        section = self._settings_provider()
        return {
            "running": self._started,
            "enabled": section.enabled,
            "engine": self._engine.name if self._engine is not None else "",
            "headless": section.headless,
            "opened": self._opened,
            "last_url": self._last.url if self._last is not None else "",
            "allow_domains": list(section.allow_domains),
            "block_domains": list(section.block_domains),
        }

    # -- helpers -----------------------------------------------------------

    def _require_enabled(self) -> BrowserSection:
        section = self._settings_provider()
        if not section.enabled:
            raise BrowserError(f"浏览器功能未启用；{_ENABLE_HINT}", details={"enabled": False})
        return section

    def _require_policy(self) -> DomainPolicy:
        if self._policy is None:
            raise BrowserError("浏览器服务未启动", details={})
        return self._policy

    def _require_engine(self) -> BrowserEngine:
        if self._engine is None:
            raise BrowserError("尚未打开任何页面", details={})
        return self._engine

    def _ensure_engine(self, section: BrowserSection) -> BrowserEngine:
        if self._engine is not None:
            return self._engine
        engine = self._override or PlaywrightEngine(
            headless=section.headless,
            timeout_seconds=section.timeout_seconds,
            max_text_chars=section.max_text_chars,
        )
        engine.start()
        self._engine = engine
        return engine
