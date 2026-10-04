"""一次系统调用拿全表，替掉逐个进程的三百次查询。

为什么存在（2026-10-02 实测，本机 336 个进程）：psutil 一次只能报一个进程，而这台机器上
装了企业端点防护之后**每个进程查询约 3 ms** —— 逐进程取 ``memory_info()`` 约 1.06 秒、
``cpu_times()`` 再约 1.07 秒，合计一趟「占用排行」要烧 2.2 秒 CPU。后果有两个，都是用户
先看见的：面板自己占掉 11.7% 的单核（而且把它自己那 2 秒算进自己那一行里），以及那一列
的采样窗口时短时长、读出来像个跳表。

任务管理器用的那个接口，``NtQuerySystemInformation(SystemProcessInformation)``，一次返回
全部进程：pid、镜像名、工作集、内核/用户时间。实测同样 336 个进程 **取 4.5 ms + 解析 3.3 ms**。

字段偏移不是猜的：``SYSTEM_PROCESS_INFORMATION`` 按 ntddk.h 的顺序用 ctypes 排出来，
``build/probe_nt_process_table.py`` 把它和 psutil 逐项对过（pid/名字/工作集/CPU 秒数）。
再加一道**自检**：第一次取表时拿本进程的 CPU 秒数和 psutil 对一次，差得离谱就判定不可用、
退回 psutil 那条老路并留一条 warning —— 结构一旦不匹配，宁可慢，不可以报假数。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("jarvis.tools.proctable")

SYSTEM_PROCESS_INFORMATION: Final[int] = 5
STATUS_INFO_LENGTH_MISMATCH: Final[int] = 0xC0000004
FILETIME_TICKS_PER_SECOND: Final[float] = 10_000_000.0
"""LARGE_INTEGER 的 100 纳秒刻度，转成秒要除的那个数。"""

SELF_CHECK_TOLERANCE_SECONDS: Final[float] = 5.0
"""自检允许的偏差。两次读数天然差几十毫秒，所以留得很宽；错的结构会差好几个数量级。"""


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _SystemProcessInformation(ctypes.Structure):
    """ntddk.h 的前半段，顺序照抄；后面用不到的字段留在缓冲里不声明。"""

    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UnicodeString),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


_ENTRY_BYTES: Final[int] = ctypes.sizeof(_SystemProcessInformation)


@dataclass(frozen=True, slots=True)
class ProcessSample:
    """One process as the system table reports it."""

    pid: int
    name: str
    working_set_bytes: int
    cpu_seconds: float
    """Cumulative CPU time since the process started -- a plain number, not a rate.

    Cumulative on purpose: a rate needs two readings, and *which* two is the caller's
    business (see ``SystemMonitor``'s rolling window). Handing back psutil's own
    per-object delta is what made the column jump.
    """


class WindowsProcessTable:
    """The whole table in one call. ``sample()`` returns ``None`` when it cannot."""

    def __init__(self) -> None:
        self._ntdll: object | None = None
        self._buffer_size = 1 << 20
        self._verified: bool | None = None

    def available(self) -> bool:
        """Whether this machine can be read this way at all."""
        if sys.platform != "win32":
            return False
        if self._ntdll is None:
            try:
                ntdll = ctypes.WinDLL("ntdll")
                ntdll.NtQuerySystemInformation.restype = ctypes.c_long
                ntdll.NtQuerySystemInformation.argtypes = [
                    ctypes.c_ulong,
                    ctypes.c_void_p,
                    ctypes.c_ulong,
                    ctypes.POINTER(ctypes.c_ulong),
                ]
            except OSError as exc:  # pragma: no cover - platform specific
                logger.warning("进程全表不可用（ntdll）：%s", exc)
                return False
            self._ntdll = ntdll
        return True

    def sample(self) -> dict[int, ProcessSample] | None:
        """Every process right now, or ``None`` to mean "use the slow path"."""
        if not self.available():
            return None
        try:
            buffer, used = self._query()
            table = self._parse(buffer, used)
        except Exception:  # pragma: no cover - a broken layout must not kill the HUD
            logger.exception("进程全表读取失败；这一步退回 psutil")
            return None
        if not table:
            return None
        if self._verified is None:
            self._verified = self._matches_psutil(table)
            if not self._verified:
                logger.warning("进程全表的自检没过（本进程 CPU 秒数和 psutil 对不上）；退回 psutil")
        if not self._verified:
            return None
        return table

    # -- internals ---------------------------------------------------------

    def _query(self) -> tuple[ctypes.Array[ctypes.c_char], int]:
        """Fetch the raw table.

        The buffer must stay referenced while parsing: ``ImageName.Buffer`` points
        *into* it, so a copy that outlives the allocation reads freed memory (实测
        第一步就是这么撞出一个 access violation 的).
        """
        assert self._ntdll is not None
        size = self._buffer_size
        for _ in range(8):
            buffer = ctypes.create_string_buffer(size)
            needed = ctypes.c_ulong(0)
            status = self._ntdll.NtQuerySystemInformation(  # type: ignore[attr-defined]
                SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(needed)
            )
            if status == STATUS_INFO_LENGTH_MISMATCH:
                size = max(size * 2, needed.value + 65_536)
                continue
            if status != 0:
                raise OSError(f"NtQuerySystemInformation 返回 0x{status & 0xFFFFFFFF:08X}")
            used = needed.value or size
            self._buffer_size = size
            return buffer, used
        raise OSError("进程全表缓冲区一直不够大")

    @staticmethod
    def _parse(buffer: ctypes.Array[ctypes.c_char], used: int) -> dict[int, ProcessSample]:
        out: dict[int, ProcessSample] = {}
        offset = 0
        while offset + _ENTRY_BYTES <= used:
            entry = _SystemProcessInformation.from_buffer(buffer, offset)
            pid = int(entry.UniqueProcessId or 0)
            if pid:
                name = ""
                if entry.ImageName.Buffer and entry.ImageName.Length:
                    raw = ctypes.string_at(entry.ImageName.Buffer, entry.ImageName.Length)
                    name = raw.decode("utf-16-le", errors="replace")
                out[pid] = ProcessSample(
                    pid=pid,
                    name=name or "?",
                    working_set_bytes=int(entry.WorkingSetSize),
                    cpu_seconds=(entry.UserTime + entry.KernelTime) / FILETIME_TICKS_PER_SECOND,
                )
            step = entry.NextEntryOffset
            if not step:
                break
            offset += step
        return out

    @staticmethod
    def _matches_psutil(table: dict[int, ProcessSample]) -> bool:
        """One known process, compared against psutil, before trusting the rest.

        Our own pid is the honest choice: it is definitely in the table, psutil can
        always read it, and its numbers change on the same machine the caller is on.
        A layout mistake shows up here as a discrepancy of hours, not milliseconds.
        """
        mine = table.get(os.getpid())
        if mine is None:
            return False
        try:
            import psutil

            proc = psutil.Process()
            cpu = float(sum(proc.cpu_times()[:2]))
            rss = int(proc.memory_info().rss)
        except Exception:  # pragma: no cover - psutil missing is handled by the caller
            return True
        close_cpu = abs(mine.cpu_seconds - cpu) <= SELF_CHECK_TOLERANCE_SECONDS
        close_rss = rss == 0 or 0.5 <= mine.working_set_bytes / rss <= 1.5
        return bool(close_cpu and close_rss)


__all__ = ["ProcessSample", "WindowsProcessTable"]
