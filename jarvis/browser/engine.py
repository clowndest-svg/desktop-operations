"""Playwright adapter for :class:`~jarvis.browser.service.BrowserService`.

``playwright`` is an optional dependency (the ``browser`` extra): it downloads a
~150 MB browser binary and must never be pulled in by a plain install. It is
therefore imported lazily, inside the function that needs it, through
``importlib``. Going through ``importlib`` keeps the heavy SDK out of this
module's import graph entirely, so the module imports cleanly on a machine that
has never heard of Playwright and the failure surfaces as a precise
:class:`BrowserError` naming the extra to install.

The browser binary itself lands in Playwright's cache on ``C:`` by default;
setting ``PLAYWRIGHT_BROWSERS_PATH`` moves it elsewhere. That advice is repeated
in the error message because a user with a small system drive hits it first.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from jarvis.browser.types import PageContent
from jarvis.core.exceptions import BrowserError

logger = logging.getLogger("jarvis.browser.engine")

_MAX_LINKS = 50
"""Cap on returned links.

There is deliberately no config key for this: a page can carry thousands of
anchors, handing them all to the model is never useful, and a fixed
conservative bound keeps a snapshot bounded regardless of the site.
"""

_INSTALL_HINT = (
    "若提示缺少浏览器内核，请运行 playwright install chromium；"
    "设置环境变量 PLAYWRIGHT_BROWSERS_PATH 可把内核安装到非系统盘，避免占用 C 盘"
)


def _missing_dependency(package: str, extra: str, exc: ImportError) -> BrowserError:
    return BrowserError(
        f"浏览器自动化需要可选的 '{package}' 依赖（安装：pip install jarvis-assistant[{extra}]）",
        details={"missing_package": package, "extra": extra},
    )


def _load_sync_playwright() -> Any:
    """Import ``playwright.sync_api.sync_playwright`` lazily.

    Split into its own function so tests can stub the whole driver with a single
    monkeypatch and never touch the real package.
    """
    try:
        module = importlib.import_module("playwright.sync_api")
    except ImportError as exc:
        raise _missing_dependency("playwright", "browser", exc) from exc
    return module.sync_playwright


class PlaywrightEngine:
    """Chromium driver built on Playwright's synchronous API."""

    def __init__(self, *, headless: bool, timeout_seconds: float, max_text_chars: int) -> None:
        """Store navigation parameters; nothing is imported or launched yet."""
        self._headless = headless
        self._timeout_ms = timeout_seconds * 1000.0
        self._max_text_chars = max_text_chars
        self._manager: Any = None
        self._browser: Any = None
        self._page: Any = None

    @property
    def name(self) -> str:
        return "playwright"

    def start(self) -> None:
        """Launch Chromium and open a blank page (idempotent)."""
        if self._page is not None:
            return
        sync_playwright = _load_sync_playwright()
        try:
            self._manager = sync_playwright().start()
            self._browser = self._manager.chromium.launch(headless=self._headless)
            self._page = self._browser.new_page()
            self._page.set_default_timeout(self._timeout_ms)
        except Exception as exc:
            # A half-started driver (a launched browser with no page) would leak
            # a process, so tear down whatever came up before reporting.
            self.stop()
            raise BrowserError(
                "启动浏览器失败",
                details={"cause": repr(exc), "hint": _INSTALL_HINT},
            ) from exc
        logger.info("playwright browser started (headless=%s)", self._headless)

    def stop(self) -> None:
        """Close page, browser and driver, ignoring teardown errors."""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:  # teardown must not mask the original failure
                logger.debug("error closing the playwright page", exc_info=True)
            self._page = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.debug("error closing the playwright browser", exc_info=True)
            self._browser = None
        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                logger.debug("error stopping the playwright driver", exc_info=True)
            self._manager = None

    def goto(self, url: str) -> PageContent:
        """Navigate to ``url`` and return the resulting page."""
        page = self._require_page()
        try:
            page.goto(url, timeout=self._timeout_ms)
        except Exception as exc:
            raise BrowserError(
                f"打开页面失败：{url}",
                details={"url": url, "cause": repr(exc)},
            ) from exc
        return self.content()

    def click(self, selector: str) -> None:
        """Click the first element matching ``selector``."""
        page = self._require_page()
        try:
            page.click(selector, timeout=self._timeout_ms)
        except Exception as exc:
            raise BrowserError(
                f"点击元素失败：{selector}",
                details={"selector": selector, "cause": repr(exc)},
            ) from exc

    def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the field matching ``selector``."""
        page = self._require_page()
        try:
            page.fill(selector, value, timeout=self._timeout_ms)
        except Exception as exc:
            raise BrowserError(
                f"填写表单失败：{selector}",
                details={"selector": selector, "cause": repr(exc)},
            ) from exc

    def screenshot(self, path: str) -> str:
        """Save a screenshot of the current page; return the path written."""
        page = self._require_page()
        try:
            page.screenshot(path=path)
        except Exception as exc:
            raise BrowserError("截图失败", details={"path": path, "cause": repr(exc)}) from exc
        return path

    def content(self) -> PageContent:
        """Read the current page without navigating."""
        page = self._require_page()
        try:
            url = str(page.url)
            title = str(page.title())
            text = str(page.inner_text("body"))
            raw_links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.innerText || '', e.href])"
            )
        except Exception as exc:
            raise BrowserError("读取页面内容失败", details={"cause": repr(exc)}) from exc
        truncated = len(text) > self._max_text_chars
        if truncated:
            text = text[: self._max_text_chars]
        links = tuple(
            (str(pair[0]), str(pair[1]))
            for pair in list(raw_links)[:_MAX_LINKS]
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        )
        return PageContent(
            url=url,
            title=title,
            text=text,
            links=links,
            truncated=truncated,
        )

    def _require_page(self) -> Any:
        if self._page is None:
            raise BrowserError("浏览器尚未启动", details={})
        return self._page
