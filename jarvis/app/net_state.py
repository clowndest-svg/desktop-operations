"""有没有网 —— 一个后台探、前台只读缓存的小东西。

三条规矩，每条都被别的坑教过：

* **``state()`` 一个网络请求都不发。** 面板每 1.5 秒读一次快照，探测放进读路径
  等于每 1.5 秒卡一次界面（投屏的"运行态 ≠ 发现态"是同一条规矩）。
* **一次失败不说"没网"。** 要所有探针都不应才算离线，而且保留上一次的答案与时间，
  界面上能写"3 秒前还有网"。
* **探的是 IP，不只探域名。** 只探域名的话，DNS 挂了和出口断了会说成同一句话，
  而这两件事用户要做的完全不一样（一个是换网络，一个是改 ``dns``）。
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final

#: (说得出名字的目标, IP, 端口)。全是公网上不要 key、不看证书、只要 TCP 握手就行的地址。
DEFAULT_TARGETS: Final[tuple[tuple[str, str, int], ...]] = (
    ("阿里 DNS", "223.5.5.5", 53),
    ("腾讯 DNS", "119.29.29.29", 53),
    ("微软连通性", "www.msftconnecttest.com", 80),
)
INTERVAL_SECONDS: Final = 20.0
CONNECT_TIMEOUT: Final = 0.8


def _probe(target: tuple[str, str, int], timeout: float) -> tuple[str, bool]:
    label, host, port = target
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return label, True
    except OSError:
        return label, False


class NetworkWatch:
    """一条守护线程按点探一次，别人只读它留下的答案。"""

    name: Final = "network"
    """``Application.register`` 按这个名字管生命周期（重复注册会被它拦下来）。"""

    def __init__(
        self,
        targets: Sequence[tuple[str, str, int]] = DEFAULT_TARGETS,
        *,
        interval: float = INTERVAL_SECONDS,
        timeout: float = CONNECT_TIMEOUT,
        probe: Callable[[tuple[str, str, int], float], tuple[str, bool]] = _probe,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._targets = tuple(targets)
        self._interval = interval
        self._timeout = timeout
        self._probe = probe
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._online: bool | None = None
        self._detail = ""
        self._checked_at = 0.0
        self._checked_once = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="jarvis-network", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.check()
            self._stop.wait(self._interval)

    # ------------------------------------------------------------------
    # 一次探测 / 一份读数
    # ------------------------------------------------------------------

    def check(self) -> dict[str, object]:
        """探一轮。**只有这条线程和测试调它，界面那一侧只读 :meth:`state`。**"""
        reached: list[str] = []
        failed: list[str] = []
        for target in self._targets:
            label, ok = self._probe(target, self._timeout)
            (reached if ok else failed).append(label)
        online = bool(reached)
        detail = f"通的：{'、'.join(reached)}" if online else "没有一个探针应答"
        if online and failed:
            detail += f"；不通的：{'、'.join(failed)}"
        with self._lock:
            self._online = online
            self._detail = detail
            self._checked_at = self._clock()
            self._checked_once = True
        return self.state()

    def state(self) -> dict[str, object]:
        """缓存的那份答案。零网络、零等待，可以放进 1.5 秒一次的轮询里。"""
        with self._lock:
            if not self._checked_once:
                # 刚起来还没探完：说"还不知道"，别说"没网"。
                return {"online": None, "detail": "还没测过", "seconds_ago": None}
            return {
                "online": bool(self._online),
                "detail": self._detail,
                "seconds_ago": round(self._clock() - self._checked_at, 1),
            }


__all__ = ["CONNECT_TIMEOUT", "DEFAULT_TARGETS", "INTERVAL_SECONDS", "NetworkWatch"]
