"""Tests for the HUD bundle's loading path (jarvis.ui.static_server + guards).

These exist because of a bug that shipped: the window loaded ``index.html`` over
``file://``, Vite emitted an ES module, and the HTML spec requires module scripts
to be fetched with CORS — so the browser blocked every script and stylesheet, and
the window rendered as an empty near-black page.

Nothing on the Python side noticed. pywebview reported a successful window, the
log said "opening desktop window", and the only error lived inside the webview's
console. The tests below are the check that was missing.
"""

from __future__ import annotations

import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jarvis.ui.desktop import (
    WEB_DIR,
    _crossorigin_attributes,
    _referenced_assets,
    _watch_for_blank_window,
    blank_window_hint,
    bundle_hint,
    index_path,
)
from jarvis.ui.static_server import HOST, BundleServer

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bundle_available() -> bool:
    """Whether a built bundle is on disk.

    ``jarvis/ui/web`` is not in git, so a checkout that has never run
    ``scripts/build_desktop.py`` has nothing to test against. Skipping is the
    honest answer there; the assertions still run in CI and for anyone building.
    """
    return index_path().is_file() and not bundle_hint()


requires_bundle = pytest.mark.skipif(
    not _bundle_available(), reason="界面产物未构建（跑 scripts/build_desktop.py）"
)


class TestBundleHint:
    def test_a_missing_bundle_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("jarvis.ui.desktop.WEB_DIR", tmp_path)
        assert "缺少" in bundle_hint()

    @requires_bundle
    def test_the_shipped_bundle_is_loadable(self) -> None:
        """The same assertion ``test_packaging_declared_deps`` makes, kept here
        too so the failure names this module when it regresses."""
        assert bundle_hint() == ""

    def test_crossorigin_in_the_html_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vite adds ``crossorigin`` to every emitted script and link. It is not
        the *only* thing that breaks ``file://`` (module scripts are CORS-fetched
        regardless), but it is a second wall in the same place, and a bundle that
        has it cannot be opened by double-clicking either."""
        (tmp_path / "index.html").write_text(
            '<script type="module" crossorigin src="./assets/a.js"></script>',
            encoding="utf-8",
        )
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "a.js").write_text("x", encoding="utf-8")
        monkeypatch.setattr("jarvis.ui.desktop.WEB_DIR", tmp_path)
        hint = bundle_hint()
        assert "crossorigin" in hint
        assert "黑" in hint

    def test_missing_asset_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "index.html").write_text(
            '<script type="module" src="./assets/gone.js"></script>', encoding="utf-8"
        )
        monkeypatch.setattr("jarvis.ui.desktop.WEB_DIR", tmp_path)
        assert "缺失" in bundle_hint()

    def test_crossorigin_attributes_are_found(self, tmp_path: Path) -> None:
        index = tmp_path / "index.html"
        index.write_text(
            '<script src="./assets/a.js" crossorigin></script>'
            '<link rel="stylesheet" crossorigin="anonymous" href="./assets/b.css">'
            "<p>crossorigin in prose is not an attribute</p>",
            encoding="utf-8",
        )
        assert len(_crossorigin_attributes(index)) == 2

    def test_clean_html_has_no_crossorigin(self, tmp_path: Path) -> None:
        index = tmp_path / "index.html"
        index.write_text('<script type="module" src="./assets/a.js"></script>', encoding="utf-8")
        assert _crossorigin_attributes(index) == ()

    @requires_bundle
    def test_the_built_bundle_has_no_crossorigin(self) -> None:
        """The regression guard: a rebuild that reintroduces the attribute fails
        here rather than on a customer's screen."""
        assert _crossorigin_attributes(index_path()) == ()

    @requires_bundle
    def test_asset_urls_are_relative(self) -> None:
        """An absolute ``/assets/...`` would resolve against the filesystem root
        once the page is opened from disk."""
        for reference in _referenced_assets(index_path()):
            assert reference.startswith("assets/"), reference
        html = index_path().read_text(encoding="utf-8")
        assert 'src="/' not in html and 'href="/' not in html

    @requires_bundle
    def test_the_bundle_is_not_an_inline_script(self) -> None:
        """A module script is the thing ``file://`` cannot run, so the loader has
        to serve the page over HTTP — which is what ``BundleServer`` is for. If a
        future build inlines everything, this test says so and the server becomes
        optional rather than load-bearing."""
        html = index_path().read_text(encoding="utf-8")
        assert 'type="module"' in html


class TestBundleServer:
    def test_serves_index_and_assets(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "a.js").write_text("console.log(1)", encoding="utf-8")
        server = BundleServer(tmp_path)
        try:
            base = server.start()
            with urllib.request.urlopen(base, timeout=10) as response:
                assert response.status == 200
                assert b"hi" in response.read()
            with urllib.request.urlopen(f"{base}assets/a.js", timeout=10) as response:
                assert b"console.log" in response.read()
        finally:
            server.stop()

    def test_binds_loopback_only(self, tmp_path: Path) -> None:
        """A desktop UI has no business being reachable from the network."""
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        server = BundleServer(tmp_path)
        try:
            assert server.start().startswith(f"http://{HOST}:")
        finally:
            server.stop()

    def test_uses_an_ephemeral_port(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        first, second = BundleServer(tmp_path), BundleServer(tmp_path)
        try:
            assert first.start() != second.start()
        finally:
            first.stop()
            second.stop()

    def test_directory_listing_is_refused(self, tmp_path: Path) -> None:
        """Listing ``assets/`` would disclose the hashed file names to anything
        that can reach the port."""
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "a.js").write_text("x", encoding="utf-8")
        server = BundleServer(tmp_path)
        try:
            with pytest.raises(urllib.error.HTTPError) as info:
                urllib.request.urlopen(f"{server.start()}assets/", timeout=10)
            assert info.value.code == 404
        finally:
            server.stop()

    def test_traversal_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "web"
        root.mkdir()
        (root / "index.html").write_text("x", encoding="utf-8")
        (tmp_path / "secret.txt").write_text("do not serve", encoding="utf-8")
        server = BundleServer(root)
        try:
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{server.start()}../secret.txt", timeout=10)
        finally:
            server.stop()

    def test_responses_are_not_cached(self, tmp_path: Path) -> None:
        """A stale ``index.html`` is exactly the bug this module exists to fix."""
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        server = BundleServer(tmp_path)
        try:
            with urllib.request.urlopen(server.start(), timeout=10) as response:
                assert response.headers["Cache-Control"] == "no-store"
        finally:
            server.stop()

    def test_start_is_idempotent_and_stop_is_safe(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        server = BundleServer(tmp_path)
        first = server.start()
        assert server.start() == first
        assert server.running is True
        server.stop()
        server.stop()
        assert server.running is False

    def test_url_before_start_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="未启动"):
            _ = BundleServer(tmp_path).url

    def test_context_manager(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        with BundleServer(tmp_path) as server:
            assert server.running is True
        assert server.running is False

    def test_missing_directory_still_binds(self, tmp_path: Path) -> None:
        """Binding must not depend on the bundle existing: ``bundle_hint`` is
        what reports that, and a server that refuses to start would turn a clear
        message into a confusing one."""
        server = BundleServer(tmp_path / "nope")
        try:
            assert server.start()
        finally:
            server.stop()

    def test_the_window_url_is_http_not_file(self, tmp_path: Path) -> None:
        """The whole point of the module: the window must not be pointed at a
        ``file://`` URL, because module scripts cannot load from one."""
        (tmp_path / "index.html").write_text("x", encoding="utf-8")
        server = BundleServer(tmp_path)
        try:
            assert not server.start().startswith("file://")
        finally:
            server.stop()


class TestBlankWindowHint:
    def test_names_the_environment_variable_to_try(self) -> None:
        """The message is the whole point: a black window used to be silent, and
        the only error lived inside the webview console."""
        hint = blank_window_hint(25.0)
        assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in hint
        assert "--no-sandbox" in hint

    def test_states_the_timeout_it_waited(self) -> None:
        assert "25 秒" in blank_window_hint(25.0)

    def test_warns_that_no_sandbox_is_a_diagnostic_not_a_setting(self) -> None:
        """Turning the sandbox off for everybody to help the few would be a
        security regression, so the message says what it costs."""
        assert "排障" in blank_window_hint(25.0)
        assert "不要长期开着" in blank_window_hint(25.0)


class TestLoadWatchdog:
    """The watchdog turns a wedged window into a message. It is the only thing
    standing between "the page did not load" and a user staring at black."""

    def _window(self) -> tuple[object, object]:
        class _Events:
            def __init__(self) -> None:
                self.handlers: list[object] = []

            def __iadd__(self, handler: object) -> _Events:
                self.handlers.append(handler)
                return self

            def fire(self) -> None:
                for handler in self.handlers:
                    handler()  # type: ignore[operator]

        window = types.SimpleNamespace(events=types.SimpleNamespace(loaded=_Events()))
        return window, window.events.loaded

    def _blank_window_probe(
        self, monkeypatch: pytest.MonkeyPatch, timeout: float
    ) -> threading.Event:
        """Make the watchdog's conclusion observable instead of waiting 25s."""
        concluded = threading.Event()

        def note(_timeout: float) -> str:
            concluded.set()
            return ""

        monkeypatch.setattr("jarvis.ui.desktop.blank_window_hint", note)
        monkeypatch.setattr("jarvis.ui.desktop.WINDOW_LOAD_TIMEOUT_SECONDS", timeout)
        return concluded

    def test_stays_quiet_when_the_page_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        concluded = self._blank_window_probe(monkeypatch, 0.2)
        window, loaded = self._window()
        _watch_for_blank_window(window)
        loaded.fire()  # type: ignore[attr-defined]
        assert not concluded.wait(0.5)

    def test_concludes_a_blank_window_when_nothing_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concluded = self._blank_window_probe(monkeypatch, 0.05)
        window, _ = self._window()
        _watch_for_blank_window(window)
        assert concluded.wait(2.0)

    def test_a_backend_without_the_event_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backend that cannot report load must not take the window down."""
        window = types.SimpleNamespace(events=types.SimpleNamespace())
        _watch_for_blank_window(window)  # must not raise


class TestLauncher:
    """``启动小夜.bat`` is what a non-technical user actually runs.

    It is not Python, so nothing else in the suite touches it — which is exactly
    how it kept a POSIX ``>/dev/null`` redirect that printed an error line on
    every launch.
    """

    def _bat(self) -> str:
        return (REPO_ROOT / "启动小夜.bat").read_text(encoding="utf-8")

    def test_exists(self) -> None:
        assert (REPO_ROOT / "启动小夜.bat").is_file()

    def test_changes_into_its_own_directory(self) -> None:
        """Double-clicking sets the working directory to the bat's folder, but a
        shortcut does not — ``%~dp0`` is what makes both work."""
        assert 'cd /d "%~dp0"' in self._bat()

    def test_checks_the_venv_before_running(self) -> None:
        assert "缺少虚拟环境" in self._bat()

    def test_checks_the_bundle_before_running(self) -> None:
        assert "缺少界面产物" in self._bat()

    def test_runs_the_desktop_entry_point(self) -> None:
        assert "-m jarvis --desktop" in self._bat()

    def test_does_not_disable_the_browser_sandbox_by_default(self) -> None:
        """``--no-sandbox`` is a diagnostic. Shipping it enabled would remove the
        renderer's sandbox for every user in order to help the few whose
        environment cannot start it — so it must stay commented out."""
        for line in self._bat().splitlines():
            stripped = line.strip()
            if "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in stripped:
                assert stripped.startswith("rem"), stripped

    def test_uses_cmd_redirect_syntax(self) -> None:
        """The POSIX null redirect is not cmd syntax: cmd treats it as a missing
        path and prints an error before anything else runs. Only executable lines
        are checked — the bat explains the mistake in a comment, and that comment
        is allowed to quote it."""
        executable = [
            line for line in self._bat().splitlines() if not line.strip().startswith("rem")
        ]
        body = "\n".join(executable)
        assert "/dev/null" not in body
        assert ">nul" in body

    def test_keeps_the_data_root_off_the_system_drive(self) -> None:
        """The speech models alone are ~900 MB; they do not belong on C:."""
        bat = self._bat()
        assert "JARVIS_HOME" in bat
        assert "if not defined" in bat, "an existing operator setting must win"

    def test_points_at_the_troubleshooting_doc(self) -> None:
        assert r"docs\桌面运维手册.md" in self._bat()

    def test_encourages_python_utf8_output(self) -> None:
        """The console prints Chinese; without a code page it is mojibake."""
        assert "chcp 65001" in self._bat()


@requires_bundle
class TestShippedBundleOverHttp:
    def test_the_real_bundle_serves_every_asset_it_references(self) -> None:
        server = BundleServer(WEB_DIR)
        try:
            base = server.start()
            for name in ("", *_referenced_assets(index_path())):
                with urllib.request.urlopen(base + name, timeout=15) as response:
                    assert response.status == 200, name
                    assert response.read(), name
        finally:
            server.stop()

    def test_the_index_declares_a_utf8_charset(self) -> None:
        """The UI is Chinese; without a declared charset a webview may guess."""
        assert "charset" in index_path().read_text(encoding="utf-8").lower()


def _page(relative: str) -> str:
    return (REPO_ROOT / "frontend" / "src" / relative).read_text(encoding="utf-8")


class TestCopyButtonsOnThePairingPanel:
    """手机接入那面板上，每一串要照抄的东西都得能复制，且失败要说得出口。

    配对码六位、证书指纹二十来个十六进制字符 —— 用手机一个字一个字敲错一位，表现是
    "连不上，不知道为什么"。这类值不给复制按钮，等于把一次输入错误的代价留给用户去猜。
    而"按了没反应"和"复制成功"必须在界面上分得开，所以断言的是三种文字都在。
    """

    PANEL = "components/MobileAccessPopup.vue"

    def test_every_hand_typed_value_has_a_copy_button(self) -> None:
        panel = _page(self.PANEL)
        for name, value in (
            ("地址", "state.url"),
            ("配对码", "code"),
            ("指纹", "state.fingerprint"),
            ("命令", "state.firewall"),
        ):
            assert f"copy('{name}', {value})" in panel, f"{value} 还要人手抄"
            assert panel.count('class="ma__copy"') >= 4

    def test_the_button_reports_three_states_not_just_success(self) -> None:
        panel = _page(self.PANEL)
        for label in ("已复制", "没复制上", "没有内容"):
            assert label in panel, f"少了「{label}」这一档，失败就看不见了"

    def test_the_marker_is_written_from_the_result_not_a_constant(self) -> None:
        """ "已复制" 只能由复制的返回值决定。

        写死的成功提示是这个项目最常见的一类假：按钮变绿了，剪贴板里什么都没有。
        """
        panel = _page(self.PANEL)
        assert "copied.value = { ...copied.value, [name]: await copyText(value) }" in panel
        assert "await copyText" not in panel.replace(
            "copied.value = { ...copied.value, [name]: await copyText(value) }", ""
        ), "第二条复制路径绕开了结果记录"

    def test_the_clipboard_helper_looks_at_the_fallback_answer(self) -> None:
        """``execCommand('copy')`` 失败时不抛异常，只返回 false。

        所以那条老路必须看返回值 —— 只 try/except 的话，浏览器会一路"复制成功"下去。
        """
        helper = _page("api/clipboard.ts")
        assert "document.execCommand('copy') ? 'copied' : 'failed'" in helper
        assert "navigator.clipboard.writeText" in helper
        assert "box.remove()" in helper, "每按一次留一个隐藏 textarea 在 DOM 里"

    def test_the_pending_timers_are_cancelled_with_the_panel(self) -> None:
        assert "onBeforeUnmount" in _page(self.PANEL)
