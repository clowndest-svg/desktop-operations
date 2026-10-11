"""起窗之前那一眼：这台机器有没有 WebView2。

这条判据不是"函数返回对不对"，是**买家那台机器上会发生什么**。pywebview 的真实行为
（`webview/platforms/winforms.py` 里读到的，不是猜的）：import 平台模块那一刻查注册表，
查不着就 `from . import mshtml as IE` + `IE._set_ie_mode()`，只留一行 `logger.warning`。
打包版 `console=False`，那行警告谁也看不见 —— 表现就是"双击后一个白窗"。

所以这里钉的是：**缺运行时的情况下绝不能走到起窗那一步**，而且提示要落在屏幕上。
"""

from __future__ import annotations

import pytest

from jarvis.ui.runtime_check import CLIENT_GUIDS, Reader, missing_message, webview2_version


def _reader(table: dict[str, str]) -> Reader:
    def read(root: str, key: str) -> str | None:
        return table.get(f"{root}|{key}")

    return read


class TestWhatCountsAsInstalled:
    def test_the_official_runtime_is_found(self) -> None:
        guid = CLIENT_GUIDS[0][1]
        key = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"
        assert webview2_version(_reader({f"HKEY_LOCAL_MACHINE|{key}": "120.0.2210.91"})) == (
            "120.0.2210.91"
        )

    def test_a_per_user_install_counts(self) -> None:
        """没有管理员权限的人装的就是 HKCU 那一份 —— 只查 HKLM 会把他判成"没装"。"""
        guid = CLIENT_GUIDS[0][1]
        key = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"
        assert webview2_version(_reader({f"HKEY_CURRENT_USER|{key}": "1.2.3.4"})) == "1.2.3.4"

    @pytest.mark.parametrize("index", [1, 2, 3])
    def test_beta_dev_canary_runtimes_also_count(self, index: int) -> None:
        """pywebview 自己认四个客户端 GUID。只查正式版会让装了 Edge Beta 的开发机开不了窗。"""
        guid = CLIENT_GUIDS[index][1]
        key = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"
        assert webview2_version(_reader({f"HKEY_LOCAL_MACHINE|{key}": "131.0.0.1"})) == "131.0.0.1"

    def test_pv_zero_means_not_installed(self) -> None:
        """`pv=0.0.0.0` 是"键在但运行时没装"的写法 —— 按字符串非空判会放过去开白窗。"""
        guid = CLIENT_GUIDS[0][1]
        key = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"
        assert webview2_version(_reader({f"HKEY_LOCAL_MACHINE|{key}": "0.0.0.0"})) == ""

    def test_nothing_registered_is_reported_as_missing(self) -> None:
        assert webview2_version(_reader({})) == ""

    def test_a_real_machine_reads_a_real_version(self) -> None:
        """跑测试这台机器上真读得到 —— 读注册表那条实现没写错的最便宜证据。"""
        assert webview2_version()[:1].isdigit()


class TestTheMessage:
    def test_the_text_names_the_next_step(self) -> None:
        text = missing_message()
        assert "webview2" in text.lower()
        assert "Bootstrapper" in text, "只说「缺运行时」等于让人自己去搜一遍"
        assert "管理员" in text, "公司机上没有管理员权限是常态，得给那条路"

    def test_the_text_explains_the_white_window(self) -> None:
        """说清"为什么不报错而是白屏"，否则用户会以为是软件坏了去重装。"""
        assert "白" in missing_message()


class TestRunRefusesToStartWithoutIt:
    def test_run_returns_before_touching_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.ui import desktop

        called: list[str] = []

        def _missing() -> bool:
            """`lambda: called.append(...) or True` 那种写法 mypy 不放过，也更难读。"""
            called.append("checked")
            return True

        monkeypatch.setattr(desktop, "warn_missing_webview2", _missing)
        monkeypatch.setattr(desktop, "BundleServer", lambda *_a, **_k: called.append("server"))
        assert desktop.run(None, None) == 3  # type: ignore[arg-type]
        assert called == ["checked"], "缺运行时时不该继续建静态服务/起窗"

    def test_a_machine_with_the_runtime_goes_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """守卫放行时后面照走 —— 判据是"走到了下一件事"，不是"没崩"。

        下一件事换成哨兵异常：真往下跑就要建静态服务、起窗口，测试里不碰那些。
        """
        from jarvis.ui import desktop

        class _Past(BaseException):
            """跑到这儿就说明运行时这一关过了。"""

        monkeypatch.setattr(desktop, "warn_missing_webview2", lambda: False)
        monkeypatch.setattr(
            desktop, "BundleServer", lambda *_a, **_k: (_ for _ in ()).throw(_Past())
        )
        with pytest.raises(_Past):
            desktop.run(None, None)  # type: ignore[arg-type]
