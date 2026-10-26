""" "检查更新"这一条：真起一个 HTTP 服务答清单，只把"发请求"那一层留成假。

判据分三半，都不能糊：

- **网络那一半用真服务器**（`ThreadingHTTPServer` 开在回环上）：404、超时、返回的不是
  JSON、返回的是数组，这几种失败在界面上必须是不同的话，而 mock 一个 `urlopen`
  恰恰会把这些分支全绕过去。
- **身份那一半钉在轮次上**。`__version__` 自这个项目第一版起就是 `0.1.0`，拿它比清单
  等于对每个买家永久说谎；真正逐轮变的只有 `rNN`，所以比的是整数控的轮次，
  而**读不出轮次时必须拒绝回答**，不许退化成 0 —— 退化成 0 会让所有买家永远"有新版"。
- **那句话就是屏上会出现的那句**，所以断言写在 `words()` 上，不写在内部字段上。
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from jarvis.app import update_check
from jarvis.app.update_check import (
    ReleaseInfo,
    fetch_release,
    parse_round,
    words,
)

MINE = "r107"


class _Handler(BaseHTTPRequestHandler):
    body = b"{}"
    status = 200
    content_type = "application/json"

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_a: object) -> None:
        """测试里不要往 stderr 刷访问日志。"""


@pytest.fixture()
def server() -> Any:
    holder = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=holder.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder
    finally:
        holder.shutdown()
        holder.server_close()


@pytest.fixture(autouse=True)
def _mine(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把「我是第几轮」捏成一个可写的数，别的都不动。"""
    holder = {"round": MINE}
    monkeypatch.setattr(update_check, "release_round", lambda: holder["round"])
    return holder


def _url(holder: ThreadingHTTPServer, path: str = "/latest.json") -> str:
    port = holder.server_address[1]
    return f"http://127.0.0.1:{port}{path}"


def _set(body: bytes, status: int = 200, content_type: str = "application/json") -> None:
    _Handler.body = body
    _Handler.status = status
    _Handler.content_type = content_type


def _manifest(**fields: object) -> bytes:
    return json.dumps(fields).encode()


class TestTheRoundIsTheIdentity:
    def test_round_labels_parse_in_every_shape_they_arrive_in(self) -> None:
        assert parse_round("r107") == 107
        assert parse_round("R107") == 107
        assert parse_round(107) == 107
        assert parse_round(" 107 ") == 107

    def test_the_compare_is_numeric_not_lexicographic(self) -> None:
        """字典序里 `"r9" > "r107"`，那会让拿到 r10 的人被告知"你这版其实更新"。"""
        low, high = parse_round("r9"), parse_round("r107")
        assert low is not None and high is not None
        assert low < high

    @pytest.mark.parametrize("junk", ["", None, "0.1.0", "latest", "r", "107a"])
    def test_unreadable_rounds_are_none_and_never_zero(self, junk: object) -> None:
        """`None` 和 `0` 是两种答案：认不出来回 0，就等于对所有人永远"有新版"。"""
        assert parse_round(junk) is None

    def test_the_machine_reading_no_round_refuses_to_answer(
        self, server: ThreadingHTTPServer, _mine: dict[str, str]
    ) -> None:
        """buildinfo 被哪天写坏了，屏上那句必须是"我读不出自己是第几轮"，不是"有新版"。"""
        _mine["round"] = ""
        _set(_manifest(round="r108", size_mb=1.0))
        info = fetch_release(_url(server))
        assert info["ok"] is False
        assert "没记下自己" in info["error"]
        assert "有新版" not in words(info)


class TestTheRoundShippedInTheBundle:
    def test_source_runs_have_no_round_even_with_a_stale_buildinfo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """源码运行不许报轮次，**哪怕这台机器上周打过一次包**。

        `build/buildinfo.json` 是上一次本地构建留下的：不挡掉它，源码里那句「我是 r107」
        就取决于这台机器干过什么，同一条测试在刚打包过的机器上和没打包过的机器上会不一样。
        """
        import jarvis

        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(jarvis, "_buildinfo", lambda: {"round": "r999"})
        assert jarvis.release_round() == ""

    def test_a_frozen_build_without_a_buildinfo_reports_no_round(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jarvis

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", r"Z:\不存在的目录", raising=False)
        assert jarvis.release_round() == ""

    def test_a_packaged_build_reads_its_own_round(self, tmp_path: pathlib.Path) -> None:
        import jarvis

        bundle = tmp_path / "jarvis"
        bundle.mkdir()
        (bundle / "buildinfo.json").write_text(
            json.dumps({"round": "r108", "built_at": "2026-10-12 04:02"}), encoding="utf-8"
        )
        monkey = pytest.MonkeyPatch()
        monkey.setattr(sys, "frozen", True, raising=False)
        monkey.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        try:
            assert jarvis.release_round() == "r108"
            # 打包版读到东西，就不该再说 "source"（那句是给浏览器预览看的）
            assert jarvis.build_stamp() == "10-12 04:02"
        finally:
            monkey.undo()

    def test_a_corrupt_buildinfo_is_not_a_round(self, tmp_path: pathlib.Path) -> None:
        import jarvis

        bundle = tmp_path / "jarvis"
        bundle.mkdir()
        (bundle / "buildinfo.json").write_text("{not json", encoding="utf-8")
        monkey = pytest.MonkeyPatch()
        monkey.setattr(sys, "frozen", True, raising=False)
        monkey.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        try:
            assert jarvis.release_round() == ""
            # 打包版坏成这样宁可回空串，也不冒充源码（"source" 会让买家以为拿到的是源码）
            assert jarvis.build_stamp() == ""
        finally:
            monkey.undo()


class TestWhatTheEndpointCanSay:
    def test_a_newer_release_is_reported_with_its_size(
        self, server: ThreadingHTTPServer, _mine: dict[str, str]
    ) -> None:
        _set(
            _manifest(
                round="r108",
                version="0.1.0",
                size_mb=9861.7,
                notes="对话里能出图了",
                sha256="abc",
            )
        )
        info = fetch_release(_url(server))
        assert info["ok"] is True and info["newer"] is True
        assert info["latest"] == "r108" and info["current"] == MINE
        assert info["size_mb"] == 9861.7
        said = words(info)
        assert "r108" in said and "r107" in said and "9861.7" in said
        assert "不会自己下载" in said, "必须当场说清这一条不会自己动"
        assert "对话里能出图了" in said, "买家问的是新版改了啥，那句得跟着一起出来"

    def test_the_current_round_is_not_told_it_is_outdated(
        self, server: ThreadingHTTPServer, _mine: dict[str, str]
    ) -> None:
        _set(_manifest(round=MINE))
        info = fetch_release(_url(server))
        assert info["ok"] is True and info["newer"] is False
        assert "没落下什么" in words(info)
        assert "有新版" not in words(info)

    def test_an_older_manifest_is_also_not_an_update(
        self, server: ThreadingHTTPServer, _mine: dict[str, str]
    ) -> None:
        """清单落后于本机（作者忘了发）时不许说"你旧了"，也不许报错。"""
        _set(_manifest(round="r99"))
        info = fetch_release(_url(server))
        assert info["ok"] is True and info["newer"] is False
        assert info["server_behind"] is True
        said = words(info)
        assert "清单" in said and "别降级" in said
        assert "也在 r99" not in said, "不许把旧清单说成最新"

    def test_a_manifest_without_a_round_is_the_authors_problem(
        self, server: ThreadingHTTPServer
    ) -> None:
        _set(_manifest(version="0.2.0", size_mb=1.0))
        info = fetch_release(_url(server))
        assert info["ok"] is False and "round" in info["error"] and "作者" in info["error"]

    def test_a_version_number_alone_is_not_an_identity(self, server: ThreadingHTTPServer) -> None:
        """把 `0.2.0` 写进 round 那一栏也一样不认 —— 认错了就是拿两套编号互相比。"""
        _set(_manifest(round="0.2.0"))
        assert fetch_release(_url(server))["ok"] is False

    def test_http_error_names_the_code_and_blames_the_right_side(
        self, server: ThreadingHTTPServer
    ) -> None:
        _set(b"nope", status=404)
        info = fetch_release(_url(server))
        assert info["ok"] is False and "404" in info["error"] and "作者服务器" in info["error"]
        # 失败也要带上自己这一轮：界面那一栏读到空串会整块不画，买家以为没装成功
        assert info["current"] == MINE

    def test_html_instead_of_json_says_the_address_is_taken(
        self, server: ThreadingHTTPServer
    ) -> None:
        _set(b"<!doctype html><title>nginx</title>", content_type="text/html")
        info = fetch_release(_url(server))
        assert info["ok"] is False and "JSON" in info["error"]

    def test_a_json_array_is_not_a_manifest(self, server: ThreadingHTTPServer) -> None:
        _set(b"[1, 2, 3]")
        assert fetch_release(_url(server))["ok"] is False

    def test_a_dead_port_does_not_raise(self) -> None:
        """没人听的时候这句话要能上屏，而不是把异常抛给桥面。

        用 opener 接缝造，而不是真连一个空端口：这台机器上有 mihomo 在截 127.0.0.1，
        真连拿到的是代理回的 502，测的就不是"连不上"这一支了。
        """
        import urllib.error

        def boom(_request: object, timeout: float = 0.0) -> None:
            raise urllib.error.URLError("Connection refused")

        info = fetch_release("http://127.0.0.1:9/latest.json", opener=boom)
        assert info["ok"] is False and "连不上" in info["error"]

    def test_the_shape_is_always_complete(self, server: ThreadingHTTPServer) -> None:
        _set(b"not json at all")
        info: ReleaseInfo = fetch_release(_url(server))
        for key in (
            "ok",
            "current",
            "latest",
            "newer",
            "server_behind",
            "notes",
            "size_mb",
            "sha256",
            "url",
            "error",
        ):
            assert key in info, f"少了 {key}：界面读成 undefined 就会整块不画"


def test_the_tool_layer_also_reports_a_stale_server_manifest() -> None:
    from jarvis.tools.builtins.update_tools import _words as tool_words

    words = tool_words(
        {
            "ok": True,
            "current": "r127",
            "latest": "r119",
            "newer": False,
            "server_behind": True,
            "size_mb": 0,
            "url": "",
        }
    )
    assert "还新" in words and "别降级" in words
    assert "就是清单上最新" not in words
