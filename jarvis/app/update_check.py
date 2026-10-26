"""问一次服务器"现在最新是哪版"：只读清单，不下任何东西。

**为什么这件事值得做**：交付包 10 GB，重发一次要买家重新下、重新校验、重新解压。
"我这版是不是旧的、旧在哪、要不要重装"是这类交付物最常见的客服第一句。一个几百字节的
静态 JSON 就能答，所以值得占一台 4 核 4G 机器上的一条 nginx。

**三条硬边界**（都是这个项目为类似的事付过账的）：

1. **只在人按一下的时候发请求**。开机自动检查 = 每一次启动都往作者服务器报一个
   "这台机器装了、什么时候在用" —— 那正是对外文档里承诺"没有遥测"要躲开的东西。
2. **绝不下载、绝不自动装**。清单里给的是 sha256 和一句"怎么拿新版"，动作留给人。
   一个会自己换自己的 exe，和买家机器上那些"静默更新"的流氓软件没有区别。
3. **失败要说清是哪一种**：没网、连不上、清单不是 JSON、版本比不了 —— 四种话不一样，
   因为用户下一步要做的事不一样（前两个查网络，后两个是作者这边的事，要能贴回来）。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Final, TypedDict

from jarvis import release_round

logger = logging.getLogger(__name__)

RELEASE_URL: Final = "http://124.220.54.64/latest.json"
"""作者的发布清单端点。写死一个 IP 而不是域名：这台机器现在只有 IP，
而"配一个可改的地址"会变成买家机器上一个没人填也没人读的字段。"""

TIMEOUT_SECONDS: Final = 6.0
USER_AGENT: Final = "xiaoye/update-check"


class ReleaseInfo(TypedDict):
    ok: bool
    current: str
    latest: str
    newer: bool
    server_behind: bool
    notes: str
    size_mb: float
    sha256: str
    url: str
    error: str


def _fail(error: str) -> ReleaseInfo:
    return {
        "ok": False,
        "current": release_round(),
        "latest": "",
        "newer": False,
        "server_behind": False,
        "notes": "",
        "size_mb": 0.0,
        "sha256": "",
        "url": "",
        "error": error,
    }


def parse_round(text: object) -> int | None:
    """``r107`` / ``R107`` / ``107`` → ``107``；认不出来回 ``None``，**不回 0**。

    两条都是从产物上量回来的，不是想当然：

    - 比**整数**不比字符串。``"r9" > "r107"`` 字典序成立，那样会告诉拿着 r10 的买家
      "你手上那版其实更新"。
    - 认不出来必须是 ``None``。拿 0 当"不知道"，任何一个真实轮次都比它大，于是
      打包配方哪天忘了把 rNN 写进 buildinfo，**所有买家都会永远看到"有新版"**，
      而没有一个人会怀疑坏的是自己这边。这一条和 ``__version__`` 那次是同一族错。
    """
    raw = str(text or "").strip().lower()
    if raw.startswith("r"):
        raw = raw[1:]
    return int(raw) if raw.isdigit() else None


def fetch_release(
    url: str = RELEASE_URL,
    opener: Callable[..., Any] | None = None,
) -> ReleaseInfo:
    """问一次发布清单。任何失败都回一份带原因的形状，不抛。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        raw = (opener or urllib.request.urlopen)(request, timeout=TIMEOUT_SECONDS)
        try:
            body = raw.read()
        finally:
            close = getattr(raw, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        return _fail(f"清单那边回了 {exc.code}（{url}）—— 这是作者服务器的问题，把这句贴回来")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return _fail(f"连不上发布清单（{url}）：{reason}。这台机器现在可能没网")
    try:
        data = json.loads(bytes(body).decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return _fail("清单不是一份 JSON —— 地址可能被别的网站占了，或服务器上的文件坏了")
    if not isinstance(data, dict):
        return _fail("清单是个 JSON 但不是对象，读不出轮次字段")

    # 身份认的是**构建轮次**，不是 `__version__`：那个常量自这个项目第一版起就是
    # 0.1.0，拿它比清单，要么人人"已是最新"，要么人人"有新版"，两种都是假话。
    mine = parse_round(release_round())
    theirs = parse_round(data.get("round"))
    if mine is None:
        return _fail(
            f"这一版没记下自己是第几轮（现在读到的是「{release_round() or '空'}」）—— "
            "源码运行本来就没有轮次；打包的 exe 读到空，是打包配方把 buildinfo 写坏了。"
            "两种都不猜新旧"
        )
    if theirs is None:
        return _fail(
            f"清单里的 round 是「{data.get('round', '')}」，不是 rNN 这样的构建轮次 —— "
            "这是作者这边清单写错了，把这句贴回来"
        )
    newer = theirs > mine
    server_behind = theirs < mine
    latest = f"r{theirs}"
    size = data.get("size_mb")
    try:
        size_mb = float(size) if size is not None else 0.0
    except (TypeError, ValueError):
        size_mb = 0.0
    return {
        "ok": True,
        "current": release_round(),
        "latest": latest,
        "newer": newer,
        "server_behind": server_behind,
        "notes": str(data.get("notes") or "")[:2000],
        "size_mb": size_mb,
        "sha256": str(data.get("sha256") or ""),
        "url": str(data.get("url") or ""),
        "error": "",
    }


def words(info: ReleaseInfo) -> str:
    """把一次检查写成一句能直接上屏的话（也是回给模型的那句）。"""
    if not info["ok"]:
        return f"没查到新版：{info['error']}"
    if not info["newer"]:
        if info.get("server_behind"):
            return (
                f"你手上这版是 {info['current']}，清单上最新的一版却是 {info['latest']} —— "
                "清单比你手上的版本还旧，作者那边的发布清单可能没同步；别降级，先把这句贴回去问作者。"
            )
        return (
            f"你手上这版是 {info['current']}，清单上最新的一版也是 {info['latest']} —— 没落下什么。"
            f"（想知道这版和上一版差在哪，看包里的 清单.txt 或问「新版改了什么」。）"
        )
    tail = f"，约 {info['size_mb']:.1f} MB" if info["size_mb"] else ""
    notes = f" 新版改了这些：{info['notes']}" if info["notes"] else ""
    where = f" 下载地址：{info['url']}" if info["url"] else ""
    return (
        f"有新版：{info['latest']}（你现在是 {info['current']}）{tail}。"
        f"软件**不会自己下载也不会自己换** —— 这一步得你点头。"
        f"{notes}{where}"
    )
