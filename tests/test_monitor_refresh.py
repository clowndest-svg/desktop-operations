"""Tests for the process-table refresh cadence in :class:`SystemMonitor`.

The ranking used to be re-walked on every snapshot. On a machine with a few hundred
processes that walk costs more than the polling interval, which turns a HUD that is
merely open into a process burning most of a core -- so the walk now has its own,
slower clock, and these tests are what keeps it that way.
"""

from __future__ import annotations

import time

import pytest

from jarvis.tools.monitor import ProcessReading, SystemMonitor, SystemSnapshot


class _FakePs:
    """Just enough of psutil for the constructor and the cheap samplers.

    Everything it does not implement raises ``AttributeError``, which the monitor
    turns into a warning string -- so ``warnings`` is never empty, and the cache
    logic has to compare *new* warnings against old rather than "any warnings".
    """

    def cpu_percent(self, interval: float | None = None, *, percpu: bool = False) -> object:
        return [0.0, 0.0] if percpu else 0.0

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
