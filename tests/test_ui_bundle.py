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

import re
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

    @requires_bundle
    def test_the_startup_chunk_stays_under_its_ceiling(self) -> None:
        """每一个字节都在首屏之前下载、解析、执行 —— 所以这里有一道上限。

        这条测试是为一个具体的失误存在的：`useChart.ts` 从 `import * as echarts from
        'echarts'` 拉整个 ECharts（柱状、饼、雷达、地图、关系图、桑基、K 线、仪表盘，
        加两套渲染器），而这一窗只画一条折线。改成 `echarts/core` 按需注册之后：

        * 改前实测 2,381,870 字节
        * 改后实测 1,753,443 字节（r111，本机 `npm run build`）
        * 那一条 import 的代价就是这两者之差：628,427 字节

        上限定在 1,900,000：比改后留了约 146 KB 给后续功能，又远低于 2,381,870
        —— 谁再把 barrel 写回来，这里当场红，而不是等用户去等一个慢窗口。
        """
        chunks = [
            WEB_DIR / reference
            for reference in _referenced_assets(index_path())
            if reference.endswith(".js")
        ]
        assert chunks, "index.html 没有引用任何 js —— 产物不对"
        biggest = max(chunks, key=lambda path: path.stat().st_size)
        size = biggest.stat().st_size
        assert size <= 1_900_000, (
            f"{biggest.name} 是 {size:,} 字节，超过上限 1,900,000。"
            "要么瘦回去，要么连同实测数字一起把上限调高并说明为什么。"
        )


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


class TestTheAlertChipIsReachable:
    """顶栏那颗「告警 N」必须点得开，而且点开之后要有东西在里面。

    r99 那版用户实拍报的就是这个：那一格是个 ``<span>``，只带一个 ``title``，
    所以"点了没反应"不是坏了 —— 是**从来就没有可点的东西**。这类 bug 静态看代码看不出来
    （数字是真的、颜色是真的），所以钉成形状断言：是按钮、事件有人接、接的地方真渲染。
    三个入口（顶栏 / 概览卡告警格 / 设置 → 告警）都落到同一张弹窗上，各自也都要有断言。
    """

    def test_the_chip_is_a_button_that_emits(self) -> None:
        bar = _page("components/TopBar.vue")
        assert "@click=\"emit('alerts')\"" in bar, "顶栏那颗 chip 又变回不能点的 span 了"
        assert "(e: 'alerts'): void" in bar, "没声明的事件名，Vue 只会静默丢掉"

    def test_the_shell_mounts_the_popup_it_opens(self) -> None:
        app = _page("App.vue")
        assert '@alerts="alertsOpen = true"' in app, "事件发出来了，没人接"
        assert '<AlertsPopup :open="alertsOpen"' in app, "接了，但没有那格弹窗"
        # 与最近动作同一层挂载：压在设置面板下面就是"看得见够不着"。
        assert app.index("<SettingsPanel") < app.index("<AlertsPopup"), "必须挂在设置之后"

    def test_the_overview_cell_opens_the_same_popup(self) -> None:
        """概览卡那一格也得点得开：旁边三格点了都开花，只有它没反应是同一形状的洞。

        用户报的是顶栏那颗，但人找这个入口的第二站就是概览卡 —— 四格长得一模一样，
        三格能点、一格不能，\"那一格是坏的\"比\"顶栏点了没反应\"更早被撞上。
        """
        grid = _page("components/OverviewGrid.vue")
        assert "@click=\"emit('alerts')\"" in grid, "概览卡告警格又变回点了没反应的 div"
        assert "(e: 'alerts'): void" in grid, "没声明的事件名，Vue 只会静默丢掉"
        # 认账那两颗必须留在原地：点「知道了」是就地记一笔，不该把整张弹窗叫出来。
        assert '@click.stop="ack(row.code)"' in grid, "行里的「知道了」会把弹窗叫出来"
        assert '@click.stop="ackAll"' in grid, "「全部知道了」会把弹窗叫出来"
        panel = _page("components/ProcessPanel.vue")
        assert "@alerts=\"emit('alerts')\"" in panel, "概览卡发出来的事件在中转站丢了"
        assert "(e: 'alerts'): void" in panel

    def test_settings_has_a_door_to_the_same_popup(self) -> None:
        """设置 → 告警里那句「看现在开着的告警」。

        弹窗自己写着"阈值在设置 → 告警里改"，这条路得回得去；App.vue 上"从设置打开"
        也才有对应物 —— 以前那句话在代码里，但设置里没有那颗按钮。
        """
        settings = _page("components/SettingsPanel.vue")
        assert "看现在开着的告警" in settings, "设置里没有通向弹窗的门"
        assert "@click=\"emit('alerts')\"" in settings, "那颗按钮的文字在，但点了没人听"
        assert "(e: 'alerts'): void" in settings
        app = _page("App.vue")
        # 三条接线必须一字不差都是同一个开关：`= false` 这种值换了名字的错，
        # count() 数不出区别，所以拿正则把三个绑定值逐字比出来。
        bindings = re.findall(r'@alerts="([^"]*)"', app)
        assert bindings == ["alertsOpen = true"] * 3, "顶栏 / 概览卡 / 设置三条路都该接同一个开关"

    def test_the_popup_names_the_number_and_the_line(self) -> None:
        popup = _page("components/AlertsPopup.vue")
        for needle in ("row.threshold", "row.unit", "row.since"):
            assert needle in popup, f"这一格缺了 {needle}：只有消息没有数，等于没解释为什么报"
        # 引擎的 count 装的是读数（第 80.6 次就是这么写上去的），不许当次数渲染。
        assert "row.count" not in popup, "count 不是次数，别把它写成「第 N 次」"
        assert 'role="dialog"' in popup and 'aria-modal="true"' in popup
        assert "acknowledgeAlert" in popup and "acknowledgeAllAlerts" in popup

    def test_acking_rereads_so_the_chip_cannot_lie(self) -> None:
        """「知道了」按下去之后这一格必须自己回读一遍。

        只发不调就是"按了没反应"：后端那条已经认了，屏上那条还挂着，
        人会再按一次、三次，然后认为这个按钮是坏的。
        """
        popup = _page("components/AlertsPopup.vue")
        for name in ("ack", "ackAll"):
            body = popup.split(f"async function {name}(")[1].split(chr(10) + "}")[0]
            assert "await load()" in body, f"{name}() 认完没回读"

    def test_the_counter_survives_the_reread(self) -> None:
        """回读那一下不许把「这一批认了几条」擦掉。

        浏览器里真按出来的：认第二条之后空态写的是「这一批 1 条」—— 因为 ``load()``
        在还有剩余行时把计数器清了。屏上少说一条，用户就以为有一颗还挂着。
        """
        popup = _page("components/AlertsPopup.vue")
        body = popup.split("async function load(")[1].split(chr(10) + "}")[0]
        assert "ackedHere" not in body, "load() 不许动这个计数，它只在弹窗打开时归零"
        assert popup.count("ackedHere.value = 0") == 1

    def test_a_volume_that_cannot_be_emptied_never_opens_an_alert(self) -> None:
        """挂载的虚拟光盘：CDFS、总共 2.5 MB、100% 已用 —— 它永远是一条消不掉的 critical。

        行为断言在 ``tests/test_app_alerts.py``；这条只钉"这个判断还在源码里"，
        因为把它删掉之后的表现是屏幕上多一颗永远消不掉的红色 chip，没人会想到是这里。
        """
        source = (REPO_ROOT / "jarvis" / "app" / "alerts.py").read_text(encoding="utf-8")
        assert "cdfs" in source.lower() and "_watchable_disk" in source


class TestTheMirrorPanelListsOneRowPerPhone:
    """投屏面板的「连着这台电脑的手机」是一台手机一行。

    第 96 轮实拍：一台 vivo 摆了三行一模一样的 V2309A —— ``adb`` 按通道列设备，插着线又开过
    两个无线端口就是三行。服务端并成一行之后，页面上还得说清楚"另外两个通道没消失"，
    不然用户以为软件把设备弄丢了。
    """

    def test_the_folded_channels_are_named_on_the_row(self) -> None:
        popup = _page("components/MirrorPopup.vue")
        assert "row.channels.length > 1" in popup, "并掉的通道没告诉用户，看着像少了两台"
        assert "row.channels.join('、')" in popup, "要说的是哪几个通道，不是「还有几个」"
        assert 'v-for="row in devices" :key="row.serial"' in popup, "一台一行，键还是那一台"

    def test_the_page_type_carries_the_channels(self) -> None:
        bridge = _page("api/bridge.ts")
        assert "channels: string[]" in bridge, "类型里没有 channels，模板那一句是编译期才炸的谎"
        assert "channels: ['MOCK-PHONE-1']" in bridge, "浏览器 mock 也得给这一格，不然预览里看不到"


class TestTheImagePanelIsReachable:
    """顶栏那颗「生图」必须点得开，而且点开之后每一格都说得出自己缺什么。

    这一轮的产品判断是"没有随包引擎就不装"，所以屏上那句话的准确性就是功能本身：
    它写「已带引擎」而包里没有，用户按下去只会得到一句看不懂的话。
    """

    def test_the_top_bar_has_a_door_and_the_shell_opens_the_popup(self) -> None:
        bar = _page("components/TopBar.vue")
        assert "@click=\"emit('images')\"" in bar, "顶栏那颗按钮点了没事件"
        assert "(e: 'images'): void" in bar, "没声明的事件名，Vue 只会静默丢掉"
        assert "生图" in bar
        app = _page("App.vue")
        assert '@images="imagesOpen = true"' in app, "事件发出来了，没人接"
        assert '<ImagePopup :open="imagesOpen"' in app, "接了，但没有那张弹窗"
        assert app.index("<MirrorPopup") < app.index("<ImagePopup"), "挂在投屏之后，同一层不打架"

    def test_the_popup_shows_the_engine_choice_and_the_two_setup_slots(self) -> None:
        popup = _page("components/ImagePopup.vue")
        for needle in (
            'v-model="engineChoice"',
            'v-model="comfyUrl"',
            'v-model="comfyDir"',
            'v-model="cloudBase"',
            'v-model="cloudModel"',
            "key_for: 'image'",
        ):
            assert needle in popup, f"生图那一格缺了 {needle}"
        assert 'role="dialog"' in popup and 'aria-modal="true"' in popup
        # 「检测引擎」和「画一张」是两扇门：探测不许混进 1.5 秒的轮询。
        assert "probeImageEngine" in popup and "generateImage" in popup
        assert "fetchImageState" in popup, "打开时不读一次状态，面板就是空壳"

    def test_the_page_declares_every_door_the_backend_answers(self) -> None:
        bridge = _page("api/bridge.ts")
        for door in (
            "image_state(): Promise<ImageState>",
            "image_probe(): Promise<ImageState>",
            "image_generate(prompt: string, size: string): Promise<ImageAnswer>",
            "image_tune(engine: string, size: string, steps: number, count: number): "
            "Promise<ImageState>",
        ):
            assert door in bridge, f"这一扇门在 Python 上有、页面声明里没有：{door}"
        assert "vendored_engine: boolean" in bridge, "包里有没带引擎必须能让面板知道"
        assert "vendored_engine: false" in bridge, "浏览器 mock 也得给这一格"

    def test_the_count_door_is_the_whole_way_through(self) -> None:
        """「一次画几张」从面板到落盘一路都得在 —— 断在哪一节都是它悄悄不生效。

        r112 加这一格的时候，云端那条的 `_ok(path, …)` 还是老签名（只收一个路径），
        整条云端路直接 `TypeError: 'WindowsPath' object is not subscriptable`。
        所以这一条不查"有没有这个字符串"，查**这条链的每一节**：
        面板有那一格 → 桥上有这个参数 → Python 有这扇门 → 服务层读得出这个偏好。
        """
        popup = _page("components/ImagePopup.vue")
        assert "一次画几张" in popup and "countChoice" in popup, "面板上没有那一格"
        assert "tuneImage(" in popup and "countChoice.value" in popup, "那一格没接到桥上"
        desk = (REPO_ROOT / "jarvis" / "ui" / "desktop.py").read_text(encoding="utf-8")
        assert "count: object = None" in desk, "桥那头不收这个参数"
        gen = (REPO_ROOT / "jarvis" / "app" / "image_gen.py").read_text(encoding="utf-8")
        assert "IMAGE_COUNT" in gen and "def set_count(" in gen, "服务层没读这个偏好"
        assert "def _parse_count(" in gen, "工具传来的张数没有解析那一步"

    def test_the_browser_mock_carries_a_whole_state(self) -> None:
        """浏览器预览里的 mock 必须像后端一样记账，不然开发模式下看到的形状是假的。"""
        bridge = _page("api/bridge.ts")
        for needle in ("mockImage.last_path =", "mockImage.phase = 'idle'", "MOCK_IMAGE_PNG"):
            assert needle in bridge, f"mock 少了 {needle}：预览里看不到真会发生的那次回写"


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


class TestComponentsCleanUpAfterThemselves:
    """挂在 window / document 上的监听得由同一个组件摘掉。

    HUD 的弹窗反复挂载卸载。顶栏那枚「手机在不在」的监听此前只 add 不 remove，每开一次
    设置就多留一个已经不在屏幕上的组件在收事件（r86 审计抓到的那条）。所以这里不钉某个
    文件，钉的是整条规矩：谁 add 谁 remove，且写在同一个 .vue 里。
    """

    def test_every_window_or_document_listener_is_removed(self) -> None:
        offenders: list[str] = []
        for vue in sorted((REPO_ROOT / "frontend" / "src" / "components").glob("*.vue")):
            source = vue.read_text(encoding="utf-8")
            for name in re.findall(
                r"(?:window|document)\.addEventListener\(\s*['\"]([^'\"]+)['\"]", source
            ):
                if (
                    f"removeEventListener('{name}'" not in source
                    and f'removeEventListener("{name}"' not in source
                ):
                    offenders.append(f"{vue.name} -> {name}")
        assert offenders == [], f"这些监听没有对应的 remove，组件卸载后还在收事件：{offenders}"

    def test_the_top_bar_unsubscribes_from_the_settings_event(self) -> None:
        """这一条是上面那条规矩第一次被破的地方，单独钉一下它到底在不在。"""
        top = _page("components/TopBar.vue")
        assert "window.addEventListener('jarvis-settings-saved', onSettingsSaved)" in top
        assert "window.removeEventListener('jarvis-settings-saved', onSettingsSaved)" in top


class TestOneTimeFormatAndTheThinkingFold:
    """用户照着截图提的三条排版/接线：时间只许一种写法、放大不许擦掉思考过程。

    写成源码级判据的理由和别的几条一样：这些是"渲染出来才知道"的东西，
    但**渲染的前提**（读的是哪个字段、有没有把内存里那份丢掉）能在源码上钉死。
    """

    def test_the_shared_stamp_is_the_only_writings_of_a_timestamp(self) -> None:
        helper = _page("api/time.ts")
        assert "getMonth() + 1" in helper and "padStart(2, '0')" in helper
        assert "Number.isNaN(date.getTime())" in helper, "读不懂的原文要原样贴出来，不许显示成 1970"
        sessions = _page("components/SessionsPopup.vue")
        assert "stamp(session.updated_at)" in sessions
        assert "{{ session.updated_at }}" not in sessions, "又把 ISO 带微秒那串直接贴到屏上"
        panel = _page("components/ChatPanel.vue")
        assert "stamp(session.updated_at)" in panel
        assert "dayOf(" not in panel, "那一格只留月-日，是第二种时间写法"

    def test_a_session_row_is_one_line(self) -> None:
        sessions = _page("components/SessionsPopup.vue")
        body = sessions[sessions.index(".ss__body {") : sessions.index(".ss__body strong")]
        assert "display: flex" in body, "标题和时间又分成两行"
        assert "min-width: 0" in body, "不给 0 下限，长标题会把按钮挤出弹窗"
        assert "max-width: 320px" not in sessions, "写死的标题宽度不跟弹窗走"

    def test_expanding_the_chat_keeps_the_thinking_chain(self) -> None:
        """「放大」也走 loadCurrent：那里以前把 reasoning 一律写空，思考过程就没了。

        第二版踩的坑更细：上一轮改成了"从 seeded 里找同一轮"，而思考过程其实活在
        voice.history 那一段里（loadCurrent 同时把 consumed 推到队尾，合并列表里
        那几行就整段丢了）。打包版量到"收起 216 字、放大后 found 0"，所以判据要钉住
        的是**从哪一份里找**，不只是"有没有带过去"。
        """
        panel = _page("components/ChatPanel.vue")
        start = panel.index("async function loadCurrent(")
        body = panel[start : panel.index("const error = computed", start)]
        assert "const previous = lines.value" in body
        assert "const previous = seeded.value" not in body, "只翻落盘那一半，思考过程在另一半里"
        assert "row.text === turn.content" in body, "不带文字比对就认不出是同一轮"
        assert "reasoning: before?.reasoning ?? ''" in body
        assert "reasoning: ''," not in body, "又无条件写空 —— 落盘的没有思考，内存里的也被擦了"
