"""Tests for the process ranking's two paths in :class:`SystemMonitor`.

The ranking used to be re-walked on every snapshot with psutil, one process at a
time. On a machine with a few hundred processes that walk costs more than the
polling interval, which turns a HUD that is merely open into a process burning
most of a core -- and it made the CPU column jump, because psutil's number is a
delta against *its own* last call on that object.

Two paths now, and both are pinned here:

* the fast one (Windows): the whole table in one call, re-read every poll, with the
  CPU share averaged over a rolling window the monitor controls (``_FakeTable``);
* the fallback (psutil): the old walk with its slower clock (``_CountingMonitor``,
  ``_WalkingPs``).
"""

from __future__ import annotations

import threading
import time
from typing import NamedTuple

import pytest

from jarvis.tools.monitor import (
    CPU_BASELINE_MAX_AGE_SECONDS,
    PROCESS_CPU_WINDOW_SECONDS,
    ProcessReading,
    SystemMonitor,
    SystemSnapshot,
)
from jarvis.tools.proctable import ProcessSample


class _CpuTimes(NamedTuple):
    """One core's cumulative times, shaped like psutil's Windows ``scputimes``."""

    user: float
    system: float
    idle: float


def _frozen_times(cores: int) -> tuple[_CpuTimes, ...]:
    """Counters that never move, for the doubles below.

    A psutil whose ``cpu_times`` stands still is a machine where no CPU time passed
    between two reads, and the monitor's answer to that is to stop differencing and
    take a sampled reading instead -- which is the path every double in this file
    was written against, so their scripted ``cpu_percent`` lines keep advancing one
    step per snapshot. The cheap differencing path has its own doubles, below.
    """
    return tuple(_CpuTimes(user=1.0, system=1.0, idle=8.0) for _ in range(cores))


class _FakeTable:
    """The whole-process-table double: pid -> (name, working set, cumulative CPU)."""

    def __init__(self, rows: dict[int, tuple[str, int, float]]) -> None:
        self.rows = rows
        self.calls = 0

    def sample(self) -> dict[int, ProcessSample]:
        self.calls += 1
        return {
            pid: ProcessSample(pid=pid, name=name, working_set_bytes=rss, cpu_seconds=cpu_seconds)
            for pid, (name, rss, cpu_seconds) in self.rows.items()
        }


class _NoTable:
    """A fast reader that cannot: what a non-Windows machine looks like."""

    def sample(self) -> None:
        return None


class _UnusablePs:
    """psutil that must not be asked for anything expensive."""

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = False) -> object:
        return [0.0] * 4 if percpu else 0.0

    def cpu_times(self, *, percpu: bool = False) -> tuple[_CpuTimes, ...]:
        return _frozen_times(4)

    def cpu_count(self) -> int:
        return 4

    def process_iter(self, attrs: object) -> list[object]:
        raise AssertionError("the fast path must not walk the table one process at a time")


class _FakePs:
    """Just enough of psutil for the constructor and the cheap samplers.

    Everything it does not implement raises ``AttributeError``, which the monitor
    turns into a warning string -- so ``warnings`` is never empty, and the cache
    logic has to compare *new* warnings against old rather than "any warnings".
    """

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = False) -> object:
        return [0.0, 0.0] if percpu else 0.0

    def cpu_times(self, *, percpu: bool = False) -> tuple[_CpuTimes, ...]:
        return _frozen_times(2)

    def cpu_count(self) -> int:
        return 4


class _CountingMonitor(SystemMonitor):
    """A monitor whose process walk counts itself and can be made to fail."""

    def __init__(self, *, fails: bool = False, **kwargs: object) -> None:
        self.calls = 0
        self._fails = fails
        super().__init__(psutil_module=_FakePs(), **kwargs)  # type: ignore[arg-type]

    def _read_top_processes(self, warnings: list[str]) -> tuple[ProcessReading, ...]:
        self.calls += 1
        if self._fails:
            warnings.append(f"processes: simulated walk failure #{self.calls}")
            return ()
        return (
            ProcessReading(
                pid=self.calls, name=f"proc-{self.calls}", cpu_percent=0.0, memory_bytes=1024
            ),
        )


def test_fast_polls_walk_the_process_table_once() -> None:
    monitor = _CountingMonitor(process_refresh_seconds=5.0)

    first = monitor.snapshot()
    monitor.snapshot()
    monitor.snapshot()

    assert monitor.calls == 1, "three quick snapshots must cost one walk, not three"
    # The very first snapshot still has to carry a ranking; a cache that starts
    # empty would show an empty table for the first TTL.
    assert [row.name for row in first.top_processes] == ["proc-1"]


def test_the_table_is_rewalked_once_the_ttl_expires() -> None:
    monitor = _CountingMonitor(process_refresh_seconds=0.05)

    monitor.snapshot()
    monitor.snapshot()
    assert monitor.calls == 1

    time.sleep(0.12)
    latest = monitor.snapshot()

    assert monitor.calls == 2
    assert [row.name for row in latest.top_processes] == ["proc-2"], "the new walk is served"


def test_a_failed_walk_is_not_cached() -> None:
    """Otherwise one transient failure freezes an empty table for the whole TTL."""
    monitor = _CountingMonitor(fails=True, process_refresh_seconds=5.0)

    monitor.snapshot()
    monitor.snapshot()

    assert monitor.calls == 2, "a walk that produced a warning must be retried immediately"


def test_top_processes_zero_skips_the_walk_entirely() -> None:
    monitor = _CountingMonitor(top_processes=0, process_refresh_seconds=5.0)

    snapshot = monitor.snapshot()

    assert snapshot.top_processes == ()
    assert monitor.calls == 0


class _FakeMemory:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _FakeProc:
    """A process whose ``cpu_percent`` is a per-object delta, the way psutil's is.

    The first call on an instance has no earlier sample to compare against and
    returns 0.0; only a *later call on the same instance* produces a number. That
    is the whole reason the monitor has to keep handles: reading the attribute out
    of ``process_iter``'s fresh ``info`` dict returned 0.0 for every process,
    forever, on every poll.
    """

    def __init__(self, pid: int, percent: float) -> None:
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": f"proc-{pid}",
            "memory_info": _FakeMemory(pid * 1024),
        }
        self._percent = percent
        self._primed = False

    def cpu_percent(self, interval: float | None = None) -> float:
        if not self._primed:
            self._primed = True
            return 0.0
        return self._percent


class _WalkingPs(_FakePs):
    """A psutil stand-in whose ``process_iter`` hands back new objects each walk."""

    def __init__(self, percents: dict[int, float]) -> None:
        self._percents = percents

    def process_iter(self, attrs: object) -> list[_FakeProc]:
        return [_FakeProc(pid, percent) for pid, percent in self._percents.items()]


def _cpu_of(snapshot: SystemSnapshot, pid: int) -> float | None:
    for row in snapshot.top_processes:
        if row.pid == pid:
            return row.cpu_percent
    raise AssertionError(f"pid {pid} missing from {snapshot.top_processes}")


def test_a_second_walk_reports_a_real_cpu_share() -> None:
    """The column was a table of zeros; this is the one line that proves it moved."""
    ps = _WalkingPs({2: 37.5, 4: 12.0})
    monitor = SystemMonitor(
        psutil_module=ps,  # type: ignore[arg-type]
        top_processes=5,
        process_refresh_seconds=0.0,
    )

    first = monitor.snapshot()
    assert _cpu_of(first, 2) is None, "a process seen for the first time has no gap to measure"

    second = monitor.snapshot()
    assert _cpu_of(second, 2) == 37.5
    assert _cpu_of(second, 4) == 12.0


def test_the_priming_walk_is_not_cached_for_the_whole_ttl() -> None:
    """A cached column of "not measured" is ten seconds of a table that looks dead.

    The TTL exists because the walk costs most of a core; this pins that the one
    extra walk at startup is spent, and that everything after it is served from
    cache as before.
    """
    ps = _WalkingPs({2: 37.5})
    monitor = SystemMonitor(
        psutil_module=ps,  # type: ignore[arg-type]
        top_processes=5,
        process_refresh_seconds=600.0,
    )

    assert _cpu_of(monitor.snapshot(), 2) is None
    # Still inside the TTL, yet the unmeasured walk must not have been frozen in.
    assert _cpu_of(monitor.snapshot(), 2) == 37.5
    # And now the TTL does its job: a third snapshot serves the cached row.
    assert _cpu_of(monitor.snapshot(), 2) == 37.5


def test_handles_of_exited_processes_are_released() -> None:
    ps = _WalkingPs({2: 10.0, 4: 10.0})
    monitor = SystemMonitor(
        psutil_module=ps,  # type: ignore[arg-type]
        top_processes=5,
        process_refresh_seconds=0.0,
    )

    monitor.snapshot()
    assert set(monitor._proc_handles) == {2, 4}

    ps._percents = {4: 10.0}
    monitor.snapshot()
    assert set(monitor._proc_handles) == {4}, "a map that only grows is the leak we measure"


class _FakeDisk:
    def __init__(self, percent: float, total: int, free: int) -> None:
        self.percent = percent
        self.total = total
        self.used = total - free
        self.free = free


class _FakePart:
    def __init__(self, mount: str) -> None:
        self.mountpoint = mount
        self.fstype = "NTFS"


class _DiskPs(_FakePs):
    def __init__(self, parts: dict[str, _FakeDisk]) -> None:
        self._parts = parts

    def disk_partitions(self, all: bool = False) -> list[_FakePart]:
        return [_FakePart(mount) for mount in self._parts]

    def disk_usage(self, mount: str) -> _FakeDisk:
        return self._parts[mount]


class TestLowSpaceWarning:
    """The C: alert, in bytes rather than percent.

    Percent cries wolf on big volumes and stays silent on small ones; the thing
    that actually runs out is bytes, so bytes are what the threshold watches.
    """

    def test_a_system_drive_under_the_line_warns_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SystemDrive", "X:")
        gb = 1024**3
        ps = _DiskPs({"X:\\": _FakeDisk(80.0, 100 * gb, 12 * gb)})

        warnings = SystemMonitor(psutil_module=ps).snapshot().warnings  # type: ignore[arg-type]

        assert any("X:" in line and "12 GB" in line for line in warnings)

    def test_a_big_drive_that_is_mostly_full_but_not_close_to_full_stays_quiet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """89% of two terabytes is two hundred gigabytes of headroom: not an alert."""
        monkeypatch.setenv("SystemDrive", "X:")
        gb = 1024**3
        ps = _DiskPs({"X:\\": _FakeDisk(89.0, 2000 * gb, 220 * gb)})

        warnings = SystemMonitor(psutil_module=ps).snapshot().warnings  # type: ignore[arg-type]

        assert not any("可用空间只剩" in line for line in warnings)

    def test_a_data_drive_under_the_line_is_not_the_system_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SystemDrive", "X:")
        gb = 1024**3
        ps = _DiskPs(
            {"X:\\": _FakeDisk(20.0, 100 * gb, 80 * gb), "E:\\": _FakeDisk(95.0, 100 * gb, 5 * gb)}
        )

        warnings = SystemMonitor(psutil_module=ps).snapshot().warnings  # type: ignore[arg-type]

        assert not any("可用空间只剩" in line for line in warnings)


class _NetPs:
    """A network counter that can be advanced, frozen, or made to go backwards.

    The attribute names are psutil's (``bytes_sent`` / ``bytes_recv``), not the
    monitor's -- a fake that invents its own names would pass while the real call
    quietly read zeros.
    """

    def __init__(self, sent: int = 0, received: int = 0) -> None:
        self._sent = sent
        self._received = received

    @property
    def bytes_sent(self) -> int:
        return self._sent

    @property
    def bytes_recv(self) -> int:
        return self._received

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = False) -> object:
        return [0.0, 0.0] if percpu else 0.0

    def cpu_times(self, *, percpu: bool = False) -> tuple[_CpuTimes, ...]:
        return _frozen_times(2)

    def cpu_count(self) -> int:
        return 2

    def net_io_counters(self) -> _NetPs:
        return self


class TestNetworkRate:
    def test_the_first_sample_reports_totals_and_no_rate(self) -> None:
        """A rate needs two samples; claiming 0 B/s on the first is a lie of the
        same shape as the CPU panel's first confident zero."""
        monitor = SystemMonitor(psutil_module=_NetPs(sent=1000, received=2000))  # type: ignore[arg-type]
        net = monitor.snapshot().net
        assert net is not None
        assert (net.sent_bytes, net.recv_bytes) == (1000, 2000)
        assert net.send_bps is None and net.receive_bps is None

    def test_a_second_sample_divides_the_difference_by_the_elapsed_time(self) -> None:
        ps = _NetPs(sent=0, received=0)
        monitor = SystemMonitor(psutil_module=ps)  # type: ignore[arg-type]
        monitor._net_sample = (time.monotonic() - 4.0, 0, 0)
        ps._sent, ps._received = 4000, 8000
        net = monitor.snapshot().net
        assert net is not None
        assert net.send_bps is not None and 900 < net.send_bps < 1100
        assert net.receive_bps is not None and 1900 < net.receive_bps < 2100

    def test_a_counter_that_went_backwards_yields_no_rate(self) -> None:
        """An interface bouncing resets its counter; a negative speed is worse than
        no speed."""
        ps = _NetPs(sent=10, received=10)
        monitor = SystemMonitor(psutil_module=ps)  # type: ignore[arg-type]
        monitor._net_sample = (time.monotonic() - 1.0, 99_999, 99_999)
        net = monitor.snapshot().net
        assert net is not None
        assert net.send_bps is None and net.receive_bps is None


class TestTheFastTablePath:
    """One call for the whole table, averaged over a window this class controls.

    The complaint this answers: the CPU column flipped between 100% and 0%. Two
    causes, both measured (``build/probe_process_cpu.py``): psutil's number covers
    whatever interval elapsed since its own last call -- a 2 s window on the priming
    walk against a 10 s window afterwards -- and the walk's own cost lands inside
    the window it is measuring, so the process doing the walking reported 74.7%
    while idle. Here the window is a rolling one the monitor computes from plain
    cumulative seconds, and every poll gets a fresh table.
    """

    @staticmethod
    def _monitor(table: object, **kwargs: object) -> SystemMonitor:
        return SystemMonitor(
            psutil_module=_UnusablePs(),  # type: ignore[arg-type]
            process_table=table,  # type: ignore[arg-type]
            top_processes=5,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_every_poll_gets_a_fresh_table(self) -> None:
        table = _FakeTable({2: ("burner", 3 * 1024**3, 0.0), 4: ("idle", 1024, 0.0)})
        monitor = self._monitor(table)

        first = monitor.snapshot()
        table.rows[2] = ("burner", 3 * 1024**3, 1.0)
        second = monitor.snapshot()

        assert table.calls == 2, "the fast path is cheap enough to run every poll"
        assert [row.pid for row in first.top_processes] == [2, 4], "ranked by memory"
        assert [row.pid for row in second.top_processes] == [2, 4]

    def test_two_readings_apart_become_a_share_of_one_core(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 100.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        table = _FakeTable({2: ("burner", 1024, 0.0)})
        monitor = self._monitor(table)

        assert _cpu_of(monitor.snapshot(), 2) is None, "one reading is not a rate"

        clock["now"] = 105.0
        table.rows[2] = ("burner", 1024, 5.0)  # five seconds of CPU in five seconds

        assert _cpu_of(monitor.snapshot(), 2) == pytest.approx(100.0)

    def test_a_half_busy_process_is_not_rounded_to_100(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        table = _FakeTable({9: ("half", 1024, 10.0)})
        monitor = self._monitor(table)
        monitor.snapshot()

        clock["now"] = 8.0
        table.rows[9] = ("half", 1024, 14.0)  # four seconds of CPU in eight

        assert _cpu_of(monitor.snapshot(), 9) == pytest.approx(50.0)

    def test_the_window_forgets_what_is_older_than_it_says(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the label "last 10 seconds" would be a claim the code does not keep."""
        clock = {"now": 0.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        table = _FakeTable({3: ("long", 1024, 0.0)})
        monitor = self._monitor(table)

        monitor.snapshot()  # t=0, cpu=0
        clock["now"] = 4.0
        table.rows[3] = ("long", 1024, 2.0)
        monitor.snapshot()  # t=4, cpu=2
        clock["now"] = PROCESS_CPU_WINDOW_SECONDS + 4.0
        table.rows[3] = ("long", 1024, 3.0)
        snapshot = monitor.snapshot()  # t=14: the t=0 sample is outside the window

        # (3 - 2) seconds of CPU over the 10 seconds that remain in the window.
        assert _cpu_of(snapshot, 3) == pytest.approx(10.0)

    def test_a_recycled_pid_does_not_inherit_the_old_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows reuses pids; a new process's cumulative time starts lower."""
        clock = {"now": 0.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        table = _FakeTable({7: ("first", 1024, 500.0)})
        monitor = self._monitor(table)
        monitor.snapshot()
        clock["now"] = 5.0
        table.rows[7] = ("first", 1024, 505.0)
        monitor.snapshot()

        clock["now"] = 8.0
        table.rows[7] = ("second", 2048, 0.2)  # same pid, brand-new process

        assert _cpu_of(monitor.snapshot(), 7) is None

    def test_an_exited_process_leaves_the_window_behind(self) -> None:
        table = _FakeTable({2: ("gone", 1024, 0.0), 4: ("stays", 512, 0.0)})
        monitor = self._monitor(table)
        monitor.snapshot()

        del table.rows[2]
        monitor.snapshot()

        assert set(monitor._proc_cpu) == {4}, "a map that only grows is the leak we measure"

    def test_no_table_means_the_psutil_path_still_answers(self) -> None:
        ps = _WalkingPs({2: 37.5})
        monitor = SystemMonitor(
            psutil_module=ps,  # type: ignore[arg-type]
            process_table=_NoTable(),
            top_processes=5,
            process_refresh_seconds=0.0,
        )

        assert _cpu_of(monitor.snapshot(), 2) is None
        assert _cpu_of(monitor.snapshot(), 2) == 37.5

    def test_a_pause_longer_than_the_window_starts_over(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The page stops polling while the window is hidden, and laptops sleep.

        Averaging across that gap would report a five-minute mean and label it "last
        10 seconds" -- so a break longer than the window drops the old baseline and
        the first frame after it says "not measured yet" instead of a wrong number.
        """
        clock = {"now": 0.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        table = _FakeTable({5: ("slept", 1024, 0.0)})
        monitor = self._monitor(table)
        monitor.snapshot()
        clock["now"] = 5.0
        table.rows[5] = ("slept", 1024, 5.0)
        assert _cpu_of(monitor.snapshot(), 5) == pytest.approx(100.0)

        clock["now"] = 5.0 + PROCESS_CPU_WINDOW_SECONDS + 1.0
        table.rows[5] = ("slept", 1024, 300.0)

        assert _cpu_of(monitor.snapshot(), 5) is None


def _spin_until(deadline: float) -> None:
    """烧一点 CPU：只为了"这台机器此刻不空闲"这一件事。"""
    x = 0
    while time.monotonic() < deadline:
        x = (x + 1) % 9973


class TestTheMachineCpuRead:
    """The ring and the per-core bars, against the real psutil.

    Reported as "cpu占用统计一下100%一下0%". The cause was not the machine: psutil keys
    ``cpu_percent(interval=None)`` to the **calling thread**, and the HUD is served by
    pywebview's thread pool -- so a poll answered by a thread that had never asked
    before read 0.0 (measured: four fresh threads read ``[100.0, 100.0, 0.0, 0.0]``,
    and ``percpu=True`` returned a row of zeros every time). No fake psutil can
    reproduce that, so this one drives the real library.

    These pin the *property* rather than the call that used to deliver it. The first
    cut asserted "every ``cpu_percent`` is asked with a positive interval", which was
    true of the fix available at the time and false of a cheaper one that holds the
    guarantee better: the monitor now keeps the baseline itself and only sleeps to
    prime it. What has to stay true is what the operator sees -- a reading on the
    first poll, and the same reading no matter which thread asked.
    """

    def test_a_fresh_monitors_first_read_is_a_reading(self) -> None:
        # 一台完全空闲的机器读出来就是 0.0 —— 那是真话，不是这条测试要抓的东西。
        # 想钉的是"第一帧就有数"，所以自己造一点负载再读：忙碌的机器不会被读成 0。
        stop = time.monotonic() + 0.4
        burn = threading.Thread(target=lambda: _spin_until(stop))
        burn.start()
        try:
            snapshot = SystemMonitor(top_processes=0).snapshot()
        finally:
            burn.join(timeout=5)

        assert snapshot.cpu.percent > 0.0, "a first poll that reads 0 is the priming zero"
        assert len(snapshot.cpu.per_core) >= 1

    def test_a_poll_from_a_brand_new_thread_still_reads_the_machine(self) -> None:
        """The reported bug, reproduced the way the thread pool caused it.

        One monitor, polled twice, the second time from a thread that has never asked
        psutil anything. Under ``interval=None`` that second read is 0.0 whatever the
        machine is doing; against a baseline the monitor owns, the thread does not
        come into it.
        """
        monitor = SystemMonitor(top_processes=0)
        stop = time.monotonic() + 0.6
        burn = threading.Thread(target=lambda: _spin_until(stop))
        burn.start()
        try:
            monitor.snapshot()  # primes the baseline, on *this* thread
            time.sleep(0.1)  # 让基线和第二次读之间真的过去一点时间
            from_pool: list[float] = []
            poller = threading.Thread(
                target=lambda: from_pool.append(monitor.snapshot().cpu.percent)
            )
            poller.start()
            poller.join(timeout=5)
        finally:
            burn.join(timeout=5)

        assert from_pool, "the second poll never finished"
        assert from_pool[0] > 0.0, "0% from a fresh thread is the thread-local baseline again"

    def test_only_the_priming_read_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 50 ms is paid once per monitor, not once per poll.

        Measured before this changed: ``snapshot()`` cost 68 ms, 50.60 ms of which was
        this sleep, every 1.5 s for as long as the window was open.
        """
        import psutil

        sampled: list[float | None] = []
        real = psutil.cpu_percent

        def spy(interval: float | None = None, *, percpu: bool = False) -> object:
            sampled.append(interval)
            return real(interval=interval, percpu=percpu)

        monkeypatch.setattr(psutil, "cpu_percent", spy)

        monitor = SystemMonitor(top_processes=0)
        monitor.snapshot()
        assert len(sampled) == 1, "the first read has to prime with a real sample"

        for _ in range(3):
            # Windows counts CPU time in ~15.6 ms ticks, so two polls in the same tick
            # have nothing to difference and fall back to sampling -- correct, and not
            # what a 1.5 s cadence looks like. Wait a few ticks, like the page does.
            time.sleep(0.1)
            monitor.snapshot()

        assert len(sampled) == 1, f"the sampled call is the priming one only, saw {sampled}"
        assert sampled[0] is not None and sampled[0] > 0, "priming must not use interval=None"
        # The interval calls above prove that subsequent polls do not sleep for
        # CPU sampling. Timing the whole snapshot also times disks/OS scheduling,
        # which can exceed 50 ms under an unrelated build or GPU generation job.

    def test_the_machine_number_is_the_mean_of_the_cores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One reading answers both, so ring and bars cannot disagree."""
        import psutil

        monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None, percpu=False: [40.0, 20.0])

        snapshot = SystemMonitor(top_processes=0).snapshot()

        assert snapshot.cpu.percent == pytest.approx(30.0)
        assert snapshot.cpu.per_core == (40.0, 20.0)


class _TickingPs:
    """A psutil whose ``cpu_times`` advance by a scripted (busy, idle) step per read.

    The opposite of the doubles at the top of this file: they freeze their counters to
    stay on the sampled path, and this one exists to drive the differencing path, where
    the number on screen comes from two rows the monitor held itself. ``cpu_percent``
    answers a value no real machine would produce, so a reading that came from the
    sampled call is unmistakable.
    """

    PRIMED = 11.0

    def __init__(self, steps: list[tuple[float, float]]) -> None:
        self._steps = steps
        self._reads = 0
        self._busy = 0.0
        self._idle = 0.0
        self.sampled = 0

    def cpu_times(self, *, percpu: bool = False) -> tuple[_CpuTimes, ...]:
        del percpu
        if self._reads < len(self._steps):
            busy, idle = self._steps[self._reads]
            self._busy += busy
            self._idle += idle
        self._reads += 1
        return (_CpuTimes(user=self._busy, system=0.0, idle=self._idle),)

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = False) -> object:
        del interval
        self.sampled += 1
        return [self.PRIMED] if percpu else self.PRIMED

    def cpu_count(self) -> int:
        return 1


class TestTheBaselineTheMonitorOwns:
    """The differencing path: what the HUD reads on every poll after the first."""

    def test_the_share_is_busy_over_total_between_two_rows(self) -> None:
        """psutil's own arithmetic, so switching paths does not move the gauge."""
        ps = _TickingPs([(0.0, 0.0), (30.0, 70.0), (90.0, 10.0)])
        monitor = SystemMonitor(top_processes=0, psutil_module=ps)  # type: ignore[arg-type]

        assert monitor.snapshot().cpu.percent == pytest.approx(ps.PRIMED)
        assert monitor.snapshot().cpu.percent == pytest.approx(30.0)
        assert monitor.snapshot().cpu.percent == pytest.approx(90.0)
        assert ps.sampled == 1, "only the priming read is allowed to sleep"

    def test_counters_that_did_not_move_are_sampled_instead_of_read_as_idle(self) -> None:
        """Two polls inside one clock tick have nothing to difference.

        ``[0.0, 0.0, 0.0]`` from three reads microseconds apart is the shape of the
        original bug; the honest answer there is to sample, not to print 0%.
        """
        ps = _TickingPs([(0.0, 0.0), (0.0, 0.0)])
        monitor = SystemMonitor(top_processes=0, psutil_module=ps)  # type: ignore[arg-type]

        monitor.snapshot()
        assert monitor.snapshot().cpu.percent == pytest.approx(ps.PRIMED)
        assert ps.sampled == 2

    def test_a_baseline_older_than_the_limit_is_thrown_away(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An average over the gap is arithmetic; printed as "现在" it is a lie.

        The machine slept, or the page stopped polling. The scripted step says the CPU
        was pinned for the whole gap -- which is true of the gap and says nothing about
        this moment, so the reading has to come from a fresh sample.
        """
        clock = {"now": 1000.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        ps = _TickingPs([(0.0, 0.0), (100.0, 0.0)])
        monitor = SystemMonitor(top_processes=0, psutil_module=ps)  # type: ignore[arg-type]

        monitor.snapshot()
        clock["now"] += CPU_BASELINE_MAX_AGE_SECONDS + 1.0

        assert monitor.snapshot().cpu.percent == pytest.approx(ps.PRIMED)
        assert ps.sampled == 2

    def test_a_gap_inside_the_limit_is_differenced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The slowest cadence the settings panel allows (60 s) still rides free."""
        clock = {"now": 1000.0}
        monkeypatch.setattr("jarvis.tools.monitor.time.monotonic", lambda: clock["now"])
        ps = _TickingPs([(0.0, 0.0), (100.0, 0.0)])
        monitor = SystemMonitor(top_processes=0, psutil_module=ps)  # type: ignore[arg-type]

        monitor.snapshot()
        clock["now"] += 60.0

        assert monitor.snapshot().cpu.percent == pytest.approx(100.0)
        assert ps.sampled == 1
