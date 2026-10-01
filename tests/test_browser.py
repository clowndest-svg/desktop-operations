"""Tests for the browser layer (jarvis.browser).

Nothing here launches a browser or opens a socket: the service is driven through
a fake engine, and the Playwright adapter is exercised against a fake page
injected at the module's lazy-import seam. That keeps the suite runnable on a
machine that has never installed the ``browser`` extra — which is exactly the
machine most contributors are on.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

import pytest

from jarvis.browser import (
    BrowserEngine,
    BrowserService,
    DomainPolicy,
    PageContent,
    PlaywrightEngine,
)
from jarvis.browser import engine as engine_module
from jarvis.config.schema import BrowserSection
from jarvis.core.exceptions import BrowserError


def _section(**overrides: object) -> BrowserSection:
    data: dict[str, object] = {
        "enabled": True,
        "headless": True,
        "timeout_seconds": 5.0,
        "max_text_chars": 100,
        "allow_domains": [],
        "block_domains": [],
    }
    data.update(overrides)
    return BrowserSection.from_mapping(data)


class _FakeEngine:
    """A scripted engine: records calls and returns canned pages."""

    def __init__(self, *, text: str = "hello", links: Sequence[tuple[str, str]] = ()) -> None:
        self._text = text
        self._links = tuple(links)
        self.started = 0
        self.stopped = 0
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.screenshots: list[str] = []
        self.url = "about:blank"
        self.title = ""

    @property
    def name(self) -> str:
        return "fake"

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def goto(self, url: str) -> PageContent:
        self.gotos.append(url)
        self.url = url
        return self.content()

    def click(self, selector: str) -> None:
        self.clicks.append(selector)

    def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    def screenshot(self, path: str) -> str:
        self.screenshots.append(path)
        return path

    def content(self) -> PageContent:
        return PageContent(
            url=self.url,
            title=self.title,
            text=self._text,
            links=self._links,
            truncated=False,
        )


class _FakePage:
    """The slice of Playwright's ``Page`` the adapter actually touches."""

    def __init__(self, *, text: str = "", links: Sequence[Sequence[str]] = ()) -> None:
        self.url = "https://example.com/"
        self._text = text
        self._links = [list(pair) for pair in links]
        self.timeouts: list[float] = []
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.shots: list[str] = []
        self.closed = False

    def set_default_timeout(self, value: float) -> None:
        self.timeouts.append(value)

    def goto(self, url: str, timeout: float | None = None) -> None:
        self.visited.append(url)
        self.url = url

    def title(self) -> str:
        return "示例页面"

    def inner_text(self, selector: str) -> str:
        return self._text

    def eval_on_selector_all(self, selector: str, expression: str) -> list[list[str]]:
        return self._links

    def click(self, selector: str, timeout: float | None = None) -> None:
        self.clicked.append(selector)

    def fill(self, selector: str, value: str, timeout: float | None = None) -> None:
        self.filled.append((selector, value))

    def screenshot(self, path: str) -> None:
        self.shots.append(path)

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser
        self.launches = 0
        self.headless: bool | None = None

    def launch(self, headless: bool) -> _FakeBrowser:
        self.launches += 1
        self.headless = headless
        return self._browser


class _FakeManager:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeSyncPlaywright:
    def __init__(self, manager: _FakeManager) -> None:
        self._manager = manager

    def start(self) -> _FakeManager:
        return self._manager


class _Driver:
    """The fake Playwright driver plus the handles a test wants to inspect."""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.browser = _FakeBrowser(page)
        self.chromium = _FakeChromium(self.browser)
        self.manager = _FakeManager(self.chromium)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = self.manager

        def sync_playwright() -> _FakeSyncPlaywright:
            return _FakeSyncPlaywright(manager)

        monkeypatch.setattr(engine_module, "_load_sync_playwright", lambda: sync_playwright)


class TestDomainPolicy:
    def test_block_wins_over_allow(self) -> None:
        """A host on both lists is refused: the block-list is the hard stop, so
        an allow-list typo can never open a door someone meant to close."""
        policy = DomainPolicy(allow=["example.com"], block=["example.com"])
        with pytest.raises(BrowserError, match="禁止访问名单"):
            policy.check("https://example.com/")

    def test_subdomain_matches_the_rule(self) -> None:
        """Users write the registrable domain; blocking ``example.com`` must also
        cover ``www.`` and any other subdomain, or the rule is trivially bypassed."""
        policy = DomainPolicy(allow=[], block=["example.com"])
        with pytest.raises(BrowserError):
            policy.check("https://www.example.com/page")
        with pytest.raises(BrowserError):
            policy.check("https://api.example.com/page")

    def test_matching_is_case_insensitive(self) -> None:
        policy = DomainPolicy(allow=["Example.COM"], block=[])
        policy.check("https://WWW.example.com/x")

    def test_empty_allow_list_permits_any_host(self) -> None:
        policy = DomainPolicy(allow=[], block=["blocked.test"])
        policy.check("https://anything.test/")

    def test_unlisted_domain_is_refused_when_an_allow_list_exists(self) -> None:
        policy = DomainPolicy(allow=["example.com"], block=[])
        with pytest.raises(BrowserError, match="不在允许访问名单"):
            policy.check("https://other.test/")

    @pytest.mark.parametrize("url", ["about:blank", "file:///etc/passwd", "javascript:alert(1)"])
    def test_url_without_a_host_is_refused(self, url: str) -> None:
        """These schemes reach local resources the allow-list cannot describe, so
        there is no host to match and the safe answer is "no"."""
        with pytest.raises(BrowserError, match="无法从该地址解析出域名"):
            DomainPolicy(allow=[], block=[]).check(url)

    def test_rules_accept_a_full_url(self) -> None:
        policy = DomainPolicy(allow=[], block=["https://example.com/path"])
        assert policy.block == ("example.com",)
        with pytest.raises(BrowserError):
            policy.check("https://example.com")


class TestPageContent:
    def test_to_dict_is_json_ready(self) -> None:
        content = PageContent(
            url="https://example.com/",
            title="标题",
            text="正文",
            links=(("更多", "https://example.com/more"),),
            truncated=True,
        )
        assert content.to_dict() == {
            "url": "https://example.com/",
            "title": "标题",
            "text": "正文",
            "links": [["更多", "https://example.com/more"]],
            "truncated": True,
        }


class TestBrowserService:
    def test_fake_engine_satisfies_the_protocol(self) -> None:
        assert isinstance(_FakeEngine(), BrowserEngine)

    def test_open_before_start_raises(self) -> None:
        service = BrowserService(lambda: _section())
        with pytest.raises(BrowserError, match="未启动"):
            service.open("https://example.com/")

    def test_disabled_service_explains_how_to_enable(self) -> None:
        """The message has to be actionable: the fix is a config flag plus an
        optional install, and the C-drive hint is what most Windows users need."""
        service = BrowserService(lambda: _section(enabled=False), engine=_FakeEngine())
        service.start()
        with pytest.raises(BrowserError) as info:
            service.open("https://example.com/")
        assert "browser.enabled=true" in str(info.value)
        assert "PLAYWRIGHT_BROWSERS_PATH" in str(info.value)

    def test_policy_is_checked_before_the_engine_starts(self) -> None:
        """A refused domain must cost nothing — no browser process, no window."""
        engine = _FakeEngine()
        service = BrowserService(lambda: _section(block_domains=["blocked.test"]), engine=engine)
        service.start()
        with pytest.raises(BrowserError, match="禁止访问名单"):
            service.open("https://blocked.test/")
        assert engine.started == 0

    def test_start_is_idempotent(self) -> None:
        service = BrowserService(lambda: _section(), engine=_FakeEngine())
        service.start()
        service.start()
        assert service.running is True

    def test_engine_is_started_once_across_navigations(self) -> None:
        engine = _FakeEngine()
        service = BrowserService(lambda: _section(), engine=engine)
        service.start()
        service.open("https://example.com/")
        service.open("https://example.com/two")
        assert engine.started == 1
        assert engine.gotos == ["https://example.com/", "https://example.com/two"]

    def test_read_before_open_raises(self) -> None:
        service = BrowserService(lambda: _section(), engine=_FakeEngine())
        service.start()
        with pytest.raises(BrowserError, match="尚未打开任何页面"):
            service.read()

    def test_click_returns_the_page_after_the_interaction(self) -> None:
        engine = _FakeEngine(text="clicked")
        service = BrowserService(lambda: _section(), engine=engine)
        service.start()
        service.open("https://example.com/")
        content = service.click("#submit")
        assert engine.clicks == ["#submit"]
        assert content.text == "clicked"

    def test_fill_forwards_selector_and_value(self) -> None:
        engine = _FakeEngine()
        service = BrowserService(lambda: _section(), engine=engine)
        service.start()
        service.open("https://example.com/")
        service.fill("#q", "查询")
        assert engine.fills == [("#q", "查询")]

    def test_screenshot_returns_the_path_written(self) -> None:
        engine = _FakeEngine()
        service = BrowserService(lambda: _section(), engine=engine)
        service.start()
        service.open("https://example.com/")
        assert service.screenshot("shot.png") == "shot.png"
        assert engine.screenshots == ["shot.png"]

    def test_stop_stops_the_engine_and_clears_state(self) -> None:
        engine = _FakeEngine()
        service = BrowserService(lambda: _section(), engine=engine)
        service.start()
        service.open("https://example.com/")
        service.stop()
        assert engine.stopped == 1
        assert service.running is False
        assert service.stats()["last_url"] == ""

    def test_default_engine_is_built_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The service must pass the configured knobs through, not its own
        guesses; a mismatch here is a headless browser popping up on a user."""
        captured: dict[str, object] = {}

        def fake_engine(**kwargs: object) -> _FakeEngine:
            captured.update(kwargs)
            return _FakeEngine()

        monkeypatch.setattr("jarvis.browser.service.PlaywrightEngine", fake_engine)
        section = _section(headless=False, timeout_seconds=3.0, max_text_chars=42)
        service = BrowserService(lambda: section)
        service.start()
        service.open("https://example.com/")
        assert captured == {"headless": False, "timeout_seconds": 3.0, "max_text_chars": 42}

    def test_stats_shape(self) -> None:
        service = BrowserService(lambda: _section(), engine=_FakeEngine())
        service.start()
        service.open("https://example.com/")
        stats = service.stats()
        assert stats["running"] is True
        assert stats["enabled"] is True
        assert stats["engine"] == "fake"
        assert stats["opened"] == 1
        assert stats["last_url"] == "https://example.com/"


class TestPlaywrightEngine:
    def test_missing_playwright_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The install hint must name the extra, otherwise the user sees a bare
        ImportError from a dependency they were never told about."""
        monkeypatch.setattr(importlib, "import_module", _boom_import)
        with pytest.raises(BrowserError, match=r"jarvis-assistant\[browser\]"):
            engine_module._load_sync_playwright()

    def test_content_before_start_raises(self) -> None:
        engine = PlaywrightEngine(headless=True, timeout_seconds=1.0, max_text_chars=10)
        with pytest.raises(BrowserError, match="尚未启动"):
            engine.content()

    def test_start_launches_once_and_sets_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _Driver(_FakePage())
        driver.install(monkeypatch)
        engine = PlaywrightEngine(headless=True, timeout_seconds=2.0, max_text_chars=100)
        engine.start()
        engine.start()
        assert driver.chromium.launches == 1
        assert driver.chromium.headless is True
        assert driver.page.timeouts == [2000.0]

    def test_goto_truncates_text_and_caps_links(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unbounded page would blow the model's context; the cap is enforced
        by the engine so no caller can forget it."""
        links = [[f"链接{i}", f"https://example.com/{i}"] for i in range(80)]
        page = _FakePage(text="字" * 250, links=links)
        driver = _Driver(page)
        driver.install(monkeypatch)
        engine = PlaywrightEngine(headless=True, timeout_seconds=2.0, max_text_chars=100)
        engine.start()
        content = engine.goto("https://example.com/")
        assert content.truncated is True
        assert len(content.text) == 100
        assert len(content.links) == 50
        assert content.title == "示例页面"

    def test_short_page_is_not_marked_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage(text="短文本")
        driver = _Driver(page)
        driver.install(monkeypatch)
        engine = PlaywrightEngine(headless=True, timeout_seconds=2.0, max_text_chars=100)
        engine.start()
        assert engine.goto("https://example.com/").truncated is False

    def test_click_fill_and_screenshot_reach_the_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage(text="ok")
        driver = _Driver(page)
        driver.install(monkeypatch)
        engine = PlaywrightEngine(headless=True, timeout_seconds=2.0, max_text_chars=100)
        engine.start()
        engine.click("#go")
        engine.fill("#q", "x")
        assert engine.screenshot("a.png") == "a.png"
        assert page.clicked == ["#go"]
        assert page.filled == [("#q", "x")]
        assert page.shots == ["a.png"]

    def test_stop_tears_the_driver_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _Driver(_FakePage())
        driver.install(monkeypatch)
        engine = PlaywrightEngine(headless=True, timeout_seconds=2.0, max_text_chars=100)
        engine.start()
        engine.stop()
        assert driver.page.closed is True
        assert driver.browser.closed is True
        assert driver.manager.stopped is True


def _boom_import(name: str) -> Any:
    raise ImportError(name)
