"""System instrumentation: the read-only half of the built-in tool set.

psutil is a third-party SDK, so by the layering rules it lives here in L2 and
never surfaces above the application layer. The UI consumes a snapshot dict
through :class:`jarvis.app.system_service.SystemService`, not this module.

Everything here is *read-only* on purpose. Mutating the machine (deleting files,
killing processes) is a different, far more dangerous concern and does not belong
in a monitoring sampler.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.core.exceptions import ToolError
from jarvis.tools.proctable import ProcessSample, WindowsProcessTable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger("jarvis.tools.monitor")

LOW_FREE_BYTES: Final[int] = 20 * 1024**3
"""Below this much free space on the system drive, the HUD says so out loud.

Percent alone is the wrong trigger: a 2 TB volume at 89% still has two hundred
gigabytes of headroom, while a 64 GB one at 70% is eighteen gigabytes from
refusing an update. Bytes are what run out, so bytes are what gets watched.
"""

_DEFAULT_TOP_PROCESSES = 8

TREND_CAPACITY: Final[int] = 1200
"""How many trend samples a monitor keeps — a memory bound, not a time bound.

At the HUD's 1.5 s cadence that is about half an hour, and at the hidden-window 10 s
cadence about three hours. The window a caller asks for is clamped to what is actually
held, so "最近十分钟卡不卡" is answered from samples that exist rather than from a
zero-filled gap. Nothing older is kept: this is a load history, not a monitoring
database, and the assistant says so when asked for a range it cannot cover.
"""

# How often the process table is actually re-read **on the psutil fallback path**.
#
# The HUD polls a snapshot every 1.5 s, and that cadence is right for CPU and
# memory -- but ``process_iter`` with ``memory_info`` opens a query handle for
# every process on the machine, and on this box (355 processes, corporate endpoint
# protection) one measured walk costs **1.353 s** against a cached read of 0.005 s.
# At the polling cadence that is 90% of a core spent doing nothing but refreshing a
# list sorted by memory, which changes on the order of minutes.
#
# Ten seconds cuts the cost to ~13% of a core and still outruns any real change in
# what is installed on the machine. The walk is also skipped entirely while the
# window is hidden (see ``stores/system.ts``), because a background assistant has no
# audience for a process table.
#
# Windows does not use this path any more -- ``jarvis.tools.proctable`` reads the
# whole table in one 8 ms call, so there is nothing worth caching. This TTL is what
# a machine without that call falls back to.
_DEFAULT_PROCESS_REFRESH_SECONDS = 10.0

PROCESS_CPU_WINDOW_SECONDS: Final[float] = 10.0
"""How far back a process's CPU share is averaged.

A rate needs two readings, and the honest window is "as far back as we have" up to
this many seconds. Two reasons it is not one poll (1.5 s): a process that bursts
every few seconds would flip between 0% and 100% on screen, which reads as a broken
dashboard even though every number is real; and psutil's own per-object deltas --
the thing this replaced -- made the window *invisible*, so the column silently
changed meaning whenever a walk was skipped. Ten seconds matches the fallback TTL,
so both paths label the same number.
"""

_PROC_CPU_SAMPLES: Final[int] = 32
"""Upper bound on samples kept per pid: the window evicts long before this does."""

CPU_SAMPLE_SECONDS: Final[float] = 0.05
"""How long one CPU reading watches, sampled inside a single call.

``cpu_percent(interval=None)`` keeps its baseline **per calling thread**, and the
HUD is served by pywebview's thread pool -- so a call from a thread that had never
asked before had no earlier sample and answered 0.0, while the next poll from a
warm thread answered the truth. Measured on this machine: four fresh threads read
``[100.0, 100.0, 0.0, 0.0]``, ``percpu=True`` returned a row of zeros every time,
and three reads microseconds apart on one thread read ``[0.0, 0.0, 0.0]``. That is
the reported "一下 100% 一下 0%", and it was never about the machine.

Asking with an explicit interval samples t1 and t2 *inside the same call*, so the
answer does not depend on who asked or when they last asked -- and the first read
of a fresh monitor is a real reading rather than a priming zero. The price is a
50 ms sleep on the polling thread against a 1.5 s cadence (and nothing at all while
the window is hidden, because the page stops polling).
"""


class Pslike(Protocol):
    """The slice of ``psutil`` this module uses, so tests can drive it directly."""

    def cpu_count(self) -> int: ...

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = ...) -> Any: ...

    def virtual_memory(self) -> Any: ...

    def swap_memory(self) -> Any: ...

    def disk_partitions(self, *, all: bool = ...) -> list[Any]: ...

    def disk_usage(self, path: str) -> Any: ...

    def boot_time(self) -> float: ...

    def net_io_counters(self) -> Any: ...

    def process_iter(self, attrs: Sequence[str]) -> list[Any]: ...

    def pid_exists(self, pid: int) -> bool: ...


class ProcessTablePort(Protocol):
    """What the fast reader has to do: the whole table, or ``None`` to fall back."""

    def sample(self) -> dict[int, ProcessSample] | None: ...


@dataclass(frozen=True, slots=True)
class CpuReading:
    percent: float
    cores: int
    per_core: tuple[float, ...] = ()
    frequencies_mhz: float | None = None


@dataclass(frozen=True, slots=True)
class MemoryReading:
    percent: float
    total_bytes: int
    used_bytes: int
    available_bytes: int
    swap_percent: float = 0.0


@dataclass(frozen=True, slots=True)
class DiskReading:
    mount: str
    fstype: str
    percent: float
    total_bytes: int
    used_bytes: int
    free_bytes: int


@dataclass(frozen=True, slots=True)
class ProcessReading:
    pid: int
    name: str
    cpu_percent: float | None
    """Share of one core, averaged over the last :data:`PROCESS_CPU_WINDOW_SECONDS`.

    ``None`` and ``0.0`` are different claims: zero says the process is idle, and a
    process that was only just discovered has not been watched for any interval at
    all. A table of confident zeros on a busy machine is the thing that reads as
    "the dashboard is broken".

    The window is stated rather than implied because it used to be neither: the
    column showed psutil's delta since *a walk that might or might not have happened
    last frame*, so the same process could read 74.7% on one paint and 16.9% two
    paints later with no change in what it was doing (measured; see
    ``build/probe_process_cpu.py``).
    """

    memory_bytes: int


@dataclass(frozen=True, slots=True)
class NetReading:
    """Bytes moved since boot, and the rate over the last interval.

    ``send_bps`` / ``receive_bps`` are ``None`` until there is a previous sample to
    difference against -- the same rule as a process's CPU share. A rate of zero and
    a rate that has not been measured are different claims, and showing ``0`` on the
    first poll is how the panel lies about an idle-looking machine.
    """

    sent_bytes: int
    recv_bytes: int
    send_bps: float | None = None
    receive_bps: float | None = None


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """One sample of the two lines the trend chart draws, plus the net rates.

    Kept in Python rather than only in the page because the assistant has to be able
    to answer "刚才那十分钟卡不卡" — and a history the model cannot read while the
    chart can is two sets of eyes over the same machine, which is the thing this whole
    round exists to remove.
    """

    taken_at: float
    cpu_percent: float | None
    """``None`` on the very first sample: there is nothing to difference against.

    The page skips that point for the same reason (``cpuReady``); recording a 0.0 here
    would draw a load cliff that never happened, and would let the model report an
    idle machine for the window that was busy.
    """

    memory_percent: float
    send_bps: float | None = None
    receive_bps: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        return {
            "taken_at": self.taken_at,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "send_bps": self.send_bps,
            "receive_bps": self.receive_bps,
        }


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """One point-in-time reading of the machine, safe to hand to the UI."""

    taken_at: float
    uptime_seconds: float
    cpu: CpuReading
    memory: MemoryReading
    disks: tuple[DiskReading, ...] = ()
    top_processes: tuple[ProcessReading, ...] = ()
    net: NetReading | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    """Per-metric failures that were skipped rather than failing the snapshot.

    A partition we cannot stat or a process that exits mid-scan is normal on
    Windows; dropping them silently would make the dashboard look healthy when
    it is only partially read.
    """

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the desktop bridge."""
        return asdict(self)


def _import_psutil() -> Pslike:
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - environment specific
        raise ToolError(
            "system monitoring requires the 'psutil' package",
            details={"missing_package": "psutil"},
        ) from exc
    return psutil  # type: ignore[no-any-return]


class SystemMonitor:
    """Samples CPU / memory / disks / top processes on demand."""

    def __init__(
        self,
        *,
        top_processes: int = _DEFAULT_TOP_PROCESSES,
        process_refresh_seconds: float = _DEFAULT_PROCESS_REFRESH_SECONDS,
        psutil_module: Pslike | None = None,
        process_table: ProcessTablePort | None = None,
    ) -> None:
        self._ps = psutil_module or _import_psutil()
        self._top = max(0, top_processes)
        self._process_ttl = max(0.0, process_refresh_seconds)
        self._process_cache: tuple[ProcessReading, ...] = ()
        self._process_cache_at = 0.0
        # One retained psutil handle per pid, because a process's CPU percentage is
        # the delta since the last call on *that object* (see _cpu_since_last_walk).
        self._proc_handles: dict[int, Any] = {}
        # The fast path: one call gives the whole table (see jarvis.tools.proctable).
        # ``None`` disables it, and keeping it off whenever a test injects its own
        # psutil double keeps those tests on the path they were written for -- a test
        # that wants the fast path injects a table double instead.
        if process_table is not None:
            self._table: ProcessTablePort | None = process_table
        else:
            self._table = None if psutil_module is not None else WindowsProcessTable()
        # pid -> [(monotonic, cumulative cpu seconds)], the rolling window the CPU
        # column is averaged over. Only the fast path fills this.
        self._proc_cpu: dict[int, deque[tuple[float, float]]] = {}
        # (monotonic, sent, received) from the previous snapshot: a rate needs two.
        self._net_sample: tuple[float, int, int] | None = None
        self._samples_taken = 0
        self._trend: deque[TrendPoint] = deque(maxlen=TREND_CAPACITY)
        self._last: SystemSnapshot | None = None

    def snapshot(self) -> SystemSnapshot:
        """Read every metric, collecting per-metric failures as warnings.

        Also the only way the trend history grows, and the only way ``latest()`` has
        anything to return: one sampler, one set of numbers, so the chart, the panel
        and the assistant's answer to "刚才卡不卡" cannot disagree.
        """
        warnings: list[str] = []
        cpu = self._read_cpu(warnings)
        memory = self._read_memory(warnings)
        disks = self._read_disks(warnings)
        processes = self._read_top_processes_cached(warnings)
        net = self._read_net(warnings)
        self._samples_taken += 1
        snapshot = SystemSnapshot(
            taken_at=time.time(),
            uptime_seconds=self._read_uptime(warnings),
            cpu=cpu,
            memory=memory,
            disks=disks,
            top_processes=processes,
            net=net,
            warnings=tuple(warnings),
        )
        self._last = snapshot
        self._trend.append(self._trend_point(snapshot))
        return snapshot

    def latest(self) -> SystemSnapshot | None:
        """The most recent reading, or ``None`` before the first one.

        Readers that must not perturb the machine take this instead of ``snapshot()``:
        a caller that constructs a fresh monitor per question used to report CPU as
        0.0% every single time, because the object that primes ``cpu_percent`` has no
        earlier sample to difference against.
        """
        return self._last

    def trend(self, minutes: float = 10.0) -> tuple[TrendPoint, ...]:
        """The recorded samples over the last ``minutes``, oldest first.

        Returns what is actually held rather than padding to the asked-for window --
        whoever calls this has to say how far back the history really reaches.
        """
        if minutes <= 0:
            return ()
        cutoff = time.time() - minutes * 60.0
        return tuple(point for point in list(self._trend) if point.taken_at >= cutoff)

    def _trend_point(self, snapshot: SystemSnapshot) -> TrendPoint:
        """Fold one snapshot into the two numbers the trend is made of.

        The first sample's CPU is recorded as ``None``, matching the rule the page
        already applies: a delta over no interval is not a load reading.
        """
        measured = snapshot.cpu.percent if self._samples_taken > 1 else None
        net = snapshot.net
        return TrendPoint(
            taken_at=snapshot.taken_at,
            cpu_percent=measured,
            memory_percent=snapshot.memory.percent,
            send_bps=net.send_bps if net is not None else None,
            receive_bps=net.receive_bps if net is not None else None,
        )

    # ------------------------------------------------------------------
    # Samplers
    # ------------------------------------------------------------------

    def _read_cpu(self, warnings: list[str]) -> CpuReading:
        """One reading per CPU plus the machine-wide share.

        The machine-wide number is the mean of the per-core ones, which is psutil's
        own definition of ``cpu_percent(percpu=False)`` -- so one sampled call answers
        both questions, and the two can never disagree on screen.
        """
        try:
            per_core = tuple(
                float(value)
                for value in self._ps.cpu_percent(interval=CPU_SAMPLE_SECONDS, percpu=True)
            )
            cores = int(self._ps.cpu_count() or 0)
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"cpu: {type(exc).__name__}: {exc}")
            return CpuReading(percent=0.0, cores=0)
        percent = sum(per_core) / len(per_core) if per_core else 0.0
        return CpuReading(percent=percent, cores=cores, per_core=per_core)

    def _read_memory(self, warnings: list[str]) -> MemoryReading:
        try:
            vm = self._ps.virtual_memory()
            swap = self._ps.swap_memory()
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"memory: {type(exc).__name__}: {exc}")
            return MemoryReading(percent=0.0, total_bytes=0, used_bytes=0, available_bytes=0)
        return MemoryReading(
            percent=float(vm.percent),
            total_bytes=int(vm.total),
            used_bytes=int(vm.total) - int(vm.available),
            available_bytes=int(vm.available),
            swap_percent=float(getattr(swap, "percent", 0.0)),
        )

    def _read_net(self, warnings: list[str]) -> NetReading | None:
        """Total bytes moved, and the rate since the previous snapshot.

        The rate is a difference, so the first call has nothing to difference
        against and reports ``None`` rather than 0 -- the same trap that made the
        CPU panel claim an idle machine for its first ten seconds (§7 of the ops
        doc). A counter that went backwards (an interface bouncing, a reboot of the
        network stack) also yields ``None`` instead of a negative number.
        """
        try:
            counters = self._ps.net_io_counters()
            sent = int(getattr(counters, "bytes_sent", 0))
            received = int(getattr(counters, "bytes_recv", 0))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"net: {type(exc).__name__}: {exc}")
            return None
        now = time.monotonic()
        previous = self._net_sample
        self._net_sample = (now, sent, received)
        if previous is None:
            return NetReading(sent_bytes=sent, recv_bytes=received)
        elapsed = now - previous[0]
        if elapsed <= 0 or sent < previous[1] or received < previous[2]:
            return NetReading(sent_bytes=sent, recv_bytes=received)
        return NetReading(
            sent_bytes=sent,
            recv_bytes=received,
            send_bps=(sent - previous[1]) / elapsed,
            receive_bps=(received - previous[2]) / elapsed,
        )

    def _read_disks(self, warnings: list[str]) -> tuple[DiskReading, ...]:
        readings: list[DiskReading] = []
        try:
            partitions = list(self._ps.disk_partitions(all=False))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"disks: {type(exc).__name__}: {exc}")
            return ()
        for part in partitions:
            try:
                usage = self._ps.disk_usage(part.mountpoint)
            except (PermissionError, OSError) as exc:
                # An empty card reader or a protected drive is routine on Windows;
                # skip that mount, but say so rather than showing a shorter list.
                warnings.append(f"disk {part.mountpoint}: {type(exc).__name__}")
                continue
            readings.append(
                DiskReading(
                    mount=str(part.mountpoint),
                    fstype=str(getattr(part, "fstype", "") or ""),
                    percent=float(usage.percent),
                    total_bytes=int(usage.total),
                    used_bytes=int(usage.used),
                    free_bytes=int(usage.free),
                )
            )
        warnings.extend(self._low_space_warnings(readings))
        return tuple(readings)

    def _read_top_processes_cached(self, warnings: list[str]) -> tuple[ProcessReading, ...]:
        """The process ranking: the full table in one call, else the psutil fallback.

        The fast path is cheap enough to run on *every* snapshot (measured 8 ms for
        336 processes against 2.2 s), so on Windows there is no cache and no TTL --
        every frame carries a fresh table and every CPU cell is averaged over the
        same rolling window. The TTL below belongs to the fallback, where a walk
        still costs whole seconds and must be spaced out.
        """
        table = self._table.sample() if self._table is not None else None
        if table is not None:
            return self._rank_from_table(table)
        return self._read_top_processes_psutil(warnings)

    def _rank_from_table(self, table: dict[int, ProcessSample]) -> tuple[ProcessReading, ...]:
        """Rank by memory, with each CPU cell averaged over the rolling window.

        The window is computed here, from plain numbers, rather than handed to psutil:
        psutil's per-process percentage is a delta against *its own* last call on that
        object, so the interval it covers depends on when the previous walk happened
        to run. That is what produced the reported 100% → 0% flapping, and no amount
        of relabelling fixes a window you do not control.
        """
        if self._top == 0:
            return ()
        now = time.monotonic()
        rows: list[ProcessReading] = []
        for pid, entry in table.items():
            history = self._proc_cpu.get(pid)
            stale = history is not None and now - history[-1][0] > PROCESS_CPU_WINDOW_SECONDS
            if history is None or stale or entry.cpu_seconds < history[-1][1]:
                # No history, a gap longer than the window (the page stops polling
                # while the window is hidden, and the machine may have slept), or the
                # pid was recycled: nothing before this is comparable, and averaging
                # across the gap would report a five-minute mean as "the last 10 s".
                history = deque(maxlen=_PROC_CPU_SAMPLES)
                self._proc_cpu[pid] = history
            history.append((now, entry.cpu_seconds))
            while len(history) > 2 and now - history[0][0] > PROCESS_CPU_WINDOW_SECONDS:
                history.popleft()
            rows.append(
                ProcessReading(
                    pid=pid,
                    name=entry.name,
                    cpu_percent=self._windowed_percent(history),
                    memory_bytes=entry.working_set_bytes,
                )
            )
        # Drop what has exited: a map that only grows is a slow leak of the very
        # thing this method measures.
        alive = set(table)
        self._proc_cpu = {pid: hist for pid, hist in self._proc_cpu.items() if pid in alive}
        rows.sort(key=lambda row: row.memory_bytes, reverse=True)
        return tuple(rows[: self._top])

    @staticmethod
    def _windowed_percent(history: deque[tuple[float, float]]) -> float | None:
        """CPU seconds over wall seconds, as a share of one core. ``None`` for one sample."""
        if len(history) < 2:
            return None
        span = history[-1][0] - history[0][0]
        if span <= 0:
            return None
        used = history[-1][1] - history[0][1]
        return max(0.0, used / span * 100.0)

    def _read_top_processes_psutil(self, warnings: list[str]) -> tuple[ProcessReading, ...]:
        """The fallback ranking: psutil, re-walked at most once per TTL.

        This is what runs where the one-call table is unavailable (non-Windows, or a
        failed self-check). Serving a stale list is the right trade here: the
        alternative is spending whole seconds of a core, forever, to show a number
        that is 1.5 seconds fresher. The cache starts empty so the first snapshot
        still carries a ranking -- the HUD would otherwise render an empty table
        until the TTL elapsed.
        """
        if self._top == 0:
            return ()
        now = time.monotonic()
        if self._process_cache and now - self._process_cache_at < self._process_ttl:
            return self._process_cache
        before = len(warnings)
        readings = self._read_top_processes(warnings)
        # Only a clean walk is worth caching. A failure must be retried on the next
        # snapshot, not frozen in for the rest of the TTL.
        #
        # And a walk where *nothing* was measured is the priming walk: every handle
        # was just created, so every CPU cell is None. Caching that would freeze a
        # column of "not measured" onto the screen for the whole TTL -- ten seconds
        # of a table that looks dead on a machine that is not. One extra walk, once,
        # at startup, is the price of the first honest paint.
        measured = not readings or any(row.cpu_percent is not None for row in readings)
        if len(warnings) == before and measured:
            self._process_cache = readings
            self._process_cache_at = now
        return readings

    def _read_top_processes(self, warnings: list[str]) -> tuple[ProcessReading, ...]:
        if self._top == 0:
            return ()
        rows: list[ProcessReading] = []
        try:
            procs = list(self._ps.process_iter(["pid", "name", "memory_info"]))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"processes: {type(exc).__name__}: {exc}")
            return ()
        seen: set[int] = set()
        for proc in procs:
            info = getattr(proc, "info", {}) or {}
            pid = int(info.get("pid") or 0)
            seen.add(pid)
            memory = info.get("memory_info")
            rows.append(
                ProcessReading(
                    pid=pid,
                    name=str(info.get("name") or "?"),
                    cpu_percent=self._cpu_since_last_walk(pid, proc),
                    memory_bytes=int(getattr(memory, "rss", 0) or 0),
                )
            )
        # Forget the handles of anything that has exited; a map that only grows
        # would eventually be a leak of the thing this method exists to measure.
        self._proc_handles = {
            pid: handle for pid, handle in self._proc_handles.items() if pid in seen
        }
        rows.sort(key=lambda row: row.memory_bytes, reverse=True)
        return tuple(rows[: self._top])

    def _cpu_since_last_walk(self, pid: int, proc: Any) -> float | None:
        """One process's CPU share, measured across the gap since the previous walk.

        ``cpu_percent`` is a delta against the last call made *on that same object*,
        and ``process_iter`` returns a brand-new object every time, so reading the
        attribute back from ``info`` yielded 0.0 forever -- the HUD's CPU column was
        a table of zeros because of it. The previous sample only exists on whatever
        handle we keep, so this walk keeps one handle per pid until the process goes.

        The first sighting of a pid has no gap to measure and reports ``None``; the
        next walk replaces it with a real delta.
        """
        held = self._proc_handles.get(pid)
        if held is None:
            try:
                proc.cpu_percent(interval=None)  # prime the baseline, discard the zero
            except Exception:  # pragma: no cover - a process that dies mid-walk
                return None
            self._proc_handles[pid] = proc
            return None
        try:
            return float(held.cpu_percent(interval=None))
        except Exception:  # pragma: no cover - exited between two walks
            self._proc_handles.pop(pid, None)
            return None

    def _low_space_warnings(self, readings: Sequence[DiskReading]) -> list[str]:
        """Name the system drive when it is close to full, in bytes not percent.

        A warning rather than a colour: the storage panel already tints a bar at
        90%, and a tint nobody is looking at is not an alert. This one reaches the
        top bar's 告警 chip, which is on screen whatever panel the eye is in.
        """
        system = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\/").upper()
        out: list[str] = []
        for reading in readings:
            if reading.mount.rstrip("\\/").upper() != system:
                continue
            if reading.free_bytes >= LOW_FREE_BYTES:
                continue
            out.append(
                f"{reading.mount} 可用空间只剩 {reading.free_bytes // 1024**3} GB"
                f"（告警线 {LOW_FREE_BYTES // 1024**3} GB）。删东西之前先扫描、勾选、确认，"
                "本工具不会自动删。"
            )
        return out

    def _read_uptime(self, warnings: list[str]) -> float:
        try:
            return max(0.0, time.time() - float(self._ps.boot_time()))
        except Exception as exc:  # pragma: no cover - platform specific
            warnings.append(f"uptime: {type(exc).__name__}: {exc}")
            return 0.0
